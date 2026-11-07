"""飞书租户标签实参覆盖守卫：高频 AlertManager.send 调用点必须传 tenant_id。

口径（Chief 派工③④）：fill/成交链与 scanner/risk/real_trading/capacity/
portfolio_allocator/execution 的**业务类**推送必须带租户标签；不传时
`humanize_alert` 落【未标记租户】，用户分不清归属——本守卫钉住已覆盖清单，
防回退与漏传。

规则：
1. 清单内（src/ 与 scripts/ 的交易链）每处 `AlertManager(...).send(` /
   `alerts.send(` 调用必须带 `tenant_id=` 实参；
2. `scripts/run_service.py` 的服务级事件（OpenD 网关、服务进程、巡检）
   **不属于**单租户——不在清单内，显式豁免并在此注释说明；
3. ml_freshness / alerts 自身（探针/通道告警）同样豁免。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 必须传 tenant_id 的文件（业务推送链）。AST 全量扫描这些文件里的 send 调用。
TENANT_TAGGED_FILES = (
    "src/futu_quant/capacity_precheck.py",
    "src/futu_quant/portfolio_allocator.py",
    "src/futu_quant/real_trading.py",
    "src/futu_quant/risk.py",
    "src/futu_quant/volatility_scanner.py",
)

#: execution.py 里豁免的行号集合不稳定，改用「函数名级」豁免：
#: 探针/服务级告警在 `submit_limit` 之外也有——但当前 execution.py 的
#: 全部业务 send 已补齐，这里按文件整体要求。
TENANT_TAGGED_FILES_ALL = TENANT_TAGGED_FILES + ("src/futu_quant/execution.py",)


def _send_calls_without_tenant(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8")
    assert text, f"{path} 为空（守卫读不到输入不能放行）"
    tree = ast.parse(text)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", "") or getattr(func, "id", "")
        if name != "send":
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        if "tenant_id" not in kwargs:
            offenders.append(node.lineno)
    return offenders


def test_business_alert_send_sites_carry_tenant_id():
    offenders: dict[str, list[int]] = {}
    for relative in TENANT_TAGGED_FILES_ALL:
        path = ROOT / relative
        hits = _send_calls_without_tenant(path)
        if hits:
            offenders[relative] = hits
    assert not offenders, (
        f"业务推送链存在未带 tenant_id 的 send 调用（飞书将显示【未标记租户】）：{offenders}"
    )


def test_service_level_alerts_documented_as_exempt():
    """run_service.py 的服务级事件显式豁免（非单租户），并注释在文件头。"""
    header = (ROOT / "tests" / "test_alert_tenant_ids.py").read_text(encoding="utf-8")
    assert "run_service.py" in header and "豁免" in header, "豁免口径必须在守卫文件头注释"


def test_alerts_and_ml_freshness_exempt():
    """通道/探针类告警（alerts.py 自身、ml_freshness）不在业务清单内。"""
    assert "src/futu_quant/alerts.py" not in TENANT_TAGGED_FILES_ALL
    assert "src/futu_quant/ml_freshness.py" not in TENANT_TAGGED_FILES_ALL


def test_mutation_dropping_one_tenant_arg_must_be_red():
    """变异：摘掉一处 tenant_id 实参 → 本守卫必红。"""
    import shutil
    import subprocess
    import sys

    module = ROOT / "src/futu_quant/volatility_scanner.py"
    original = module.read_text(encoding="utf-8")
    anchor = ('incident_key=f"volatility-candidate-filtered:{tenant_id}:{symbol}",\n'
              '            tenant_id=tenant_id, benign=True,\n        )')
    assert anchor in original, "tenant_id 实参锚点丢失，无法构造变异"
    mutated = original.replace(anchor, 'incident_key=f"volatility-candidate-filtered:{tenant_id}:{symbol}",\n        )')
    try:
        module.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_business_alert_send_sites_carry_tenant_id"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "摘掉 tenant_id 后守卫仍绿 —— 租户标签可被静默移除"
    finally:
        module.write_text(original, encoding="utf-8")
        shutil.rmtree(".pytest_cache", ignore_errors=True)
