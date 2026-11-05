"""飞书推送租户标签守卫（用户 2026-11-06 拍板）。

用户原话痛点：飞书信息看不懂哪个是实盘的成交、哪个是模拟盘的。
契约：所有飞书推送（告警卡片/恢复卡片/复盘战报）必须带【实盘】/【模拟盘】
租户标签，且标签只能出自 `feishu_humanize.tenant_tag`（唯一出口，禁止各自拼）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_tenant_tag_maps_env_to_label():
    from futu_quant.feishu_humanize import tenant_tag

    assert tenant_tag("futu_real_master") == "【实盘】"
    assert tenant_tag("futu_paper_sandbox") == "【模拟盘】"
    assert tenant_tag("moomoo_real") == "【实盘】"
    assert tenant_tag("") == "【未标记租户】", "空租户必须显式可见，禁止静默丢标签"
    assert tenant_tag(None) == "【未标记租户】"
    assert tenant_tag("unknown_tenant") == "【unknown_tenant】", (
        "未注册租户带原名标签，绝不静默归属"
    )


def test_humanize_alert_title_carries_tenant_tag():
    from futu_quant.feishu_humanize import humanize_alert

    for tenant, expected in (
        ("futu_real_master", "【实盘】"),
        ("futu_paper_sandbox", "【模拟盘】"),
    ):
        for recovered in (False, True):
            title, _ = humanize_alert(
                "RISK_REJECTED", "ERROR", "测试消息",
                is_recovered=recovered, tenant_id=tenant,
            )
            assert title.startswith(expected), (
                f"tenant={tenant} recovered={recovered} 标题缺租户标签: {title}"
            )
    # 不传租户 → 未标记标签（显式可见，不是无标签）
    title, _ = humanize_alert("RISK_REJECTED", "ERROR", "x")
    assert title.startswith("【未标记租户】")


def test_alerts_send_and_recover_accept_tenant_id():
    """AlertManager.send/recover 必须有 tenant_id 形参并传给飞书卡片。"""
    source = (ROOT / "src/futu_quant/alerts.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    by_name = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    for name in ("send", "recover", "_push_feishu"):
        fn = by_name.get(name)
        assert fn is not None, f"alerts.py 缺 {name}"
        assert "tenant_id" in [a.arg for a in fn.args.args], (
            f"AlertManager.{name} 缺 tenant_id 形参"
        )
    # _push_feishu 必须把 tenant_id 传给 send_alert_card
    push_src = ast.get_source_segment(source, by_name["_push_feishu"])
    assert "tenant_id=tenant_id" in push_src, "_push_feishu 未把租户传给 send_alert_card"


def test_review_report_dual_track_lines_carry_tags():
    """复盘战报双轨核算行必须带租户标签。"""
    source = (ROOT / "src/futu_quant/feishu_reporter.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "dispatch_pending_review"
    )
    seg = ast.get_source_segment(source, fn)
    assert "tenant_tag(" in seg, "复盘战报未调用唯一出口 tenant_tag"
    assert "实盘战区" in seg and "模拟战区" in seg


def test_mutation_dropping_tag_must_be_red():
    """变异守卫：tenant_tag 把实盘标签删掉时本文件用例必须红。"""
    import shutil
    import subprocess
    import sys

    module = ROOT / "src/futu_quant/feishu_humanize.py"
    original = module.read_text(encoding="utf-8")
    anchor = '        return "【实盘】"'
    assert anchor in original, "实盘标签锚点丢失，无法构造变异"
    mutated = original.replace(anchor, '        return ""')
    try:
        module.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_tenant_tag_maps_env_to_label"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "删掉实盘标签后守卫仍绿 —— 租户标签可被静默移除"
    finally:
        module.write_text(original, encoding="utf-8")
        shutil.rmtree(".pytest_cache", ignore_errors=True)
