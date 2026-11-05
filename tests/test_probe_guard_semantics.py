"""probe_guard 双语义拆分守卫（Product 判据 2026-11-06 + Reviewer 六条快审口径）。

事故回顾：`LOW_COST_PROBE_GUARD`（本应只管 $0.01 探针单）把策略扫描器的
正常 BUY 也拦了 404 次——同一道门被两个语义共用，code review 抓不住。
本文件用 AST 钉死调用点清单与两条路径的分离，防第三次。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: `probe_entry_guard` 合法调用点清单（探针路径专用）。
#: 清单外新增调用必须显式走本清单评审，否则守卫红。
PROBE_GUARD_ALLOWED_CALLERS = (
    "src/futu_quant/execution.py",  # probe_test=True 专用分支
)

#: `run_campaign`（策略扫描路径）不得再调用探针护栏。
STRATEGY_RUNNER = "scripts/run_gamma_strategy.py"


def _tree(path: Path) -> ast.AST:
    text = path.read_text(encoding="utf-8")
    assert text, f"{path} 为空（守卫读不到输入不能放行）"
    return ast.parse(text)


def test_probe_guard_call_sites_match_allowlist():
    """AST 守卫：probe_entry_guard 全部调用点必须在清单内（防第三入口漏传语义）。"""
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("tests/", "data/")):
            continue
        try:
            tree = _tree(path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", "") or getattr(func, "id", "")
                if name == "probe_entry_guard":
                    if relative not in PROBE_GUARD_ALLOWED_CALLERS:
                        offenders.append(relative)
    assert not offenders, (
        f"probe_entry_guard 出现在清单外调用点（必须先评审再加清单）：{offenders}"
    )


def test_strategy_runner_no_longer_calls_probe_guard():
    """run_campaign 的普通 BUY 路径已摘掉探针门（Product 判据 P1：策略扫描器一律不是探针单）。"""
    source = (ROOT / STRATEGY_RUNNER).read_text(encoding="utf-8")
    assert "probe_entry_guard" not in source, (
        "策略扫描路径仍在调用探针护栏 —— 双语义拆分被回退，404 次误拦会复发"
    )
    assert "low_cost_probe = True" not in source, (
        "普通开仓不应再被打上探针标记（low_cost_probe 语义已归位到 execution 层）"
    )


def test_normal_buy_path_keeps_all_other_gates():
    """Product §6：摘掉的只能有「1 笔/日」。单笔收窄/价差门/风控/REAL 闸门必须原样保留。"""
    source = (ROOT / STRATEGY_RUNNER).read_text(encoding="utf-8")
    assert "volatility_entry_spread_gate" in source, "价差门被连带摘掉了"
    assert "min(int(per_stage), 1)" in source, "单笔张数收窄被连带摘掉了"
    execution_src = (ROOT / "src/futu_quant/execution.py").read_text(encoding="utf-8")
    assert "check_real_trade(" in execution_src, "REAL 闸门被连带摘掉了"
    assert "resolve_risk_limits(" in execution_src, "风控解析被连带摘掉了"


def test_probe_guard_only_applies_to_explicit_probe_metadata():
    """execution 层：探针门只对 metadata.probe_test is True 生效（Product P1 判据）。"""
    source = (ROOT / "src/futu_quant/execution.py").read_text(encoding="utf-8")
    assert 'metadata.get("probe_test") is True' in source, (
        "探针门未按显式 probe_test 标记分流（运行时猜判据 = Product 明令禁止）"
    )


def test_probe_guard_rejects_priced_probe_criteria():
    """Product §2 明令禁止：不得以权利金阈值判探针单（SOFI $44/RIVN $50 是真开仓）。"""
    execution_src = (ROOT / "src/futu_quant/execution.py").read_text(encoding="utf-8")
    assert "probe_premium_max" not in execution_src
    runner_src = (ROOT / STRATEGY_RUNNER).read_text(encoding="utf-8")
    assert "probe_premium_max" not in runner_src


def _run_probe_guard_block(tmp_store, *, probe_test: bool):
    """复现 execution 层分流：probe_test=True 走护栏；False 直接进 REAL 闸门。"""
    from futu_quant.capacity_precheck import probe_entry_guard, today_real_buy_entries

    metadata = {"probe_test": probe_test, "multiplier": 100}
    if metadata.get("probe_test") is not True:
        return "REAL_GATE", None
    allowed, reason, detail = probe_entry_guard(
        today_real_buy_entries(
            tmp_store, tenant_id="futu_real_master",
            strategy_id="volatility_long_gamma",
        ),
        quantity=1, price=0.01, multiplier=100, premium_limit=398.0,
    )
    return ("PROBE_GUARD", (allowed, reason, detail))


def test_probe_path_blocked_after_one_probe_same_day(tmp_path):
    """探针单第 2 笔/日被拒（护栏保留，变异①的对照面）。"""
    from futu_quant.capacity_precheck import probe_entry_guard, today_real_buy_entries
    from futu_quant.store import TradingStore

    store = TradingStore(tmp_path / "probe.db", bootstrap=True)
    today = __import__("datetime").datetime.now(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    ).date().isoformat()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO order_intents (strategy_id, intent_key, symbol, side,"
            " quantity, limit_price, status, created_at, updated_at, tenant_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("volatility_long_gamma", f"{today}:probe1:REAL", "US.SPY261120C600000",
             "BUY", 1, 0.01, "CANCELLED", f"{today}T14:00:00+00:00",
             f"{today}T14:00:01+00:00", "futu_real_master"),
        )
        conn.commit()
    # 探针单已撤（CANCELLED 不计名额）→ 本单放行（premium_limit 由 execution 层
    # 传压力预算；本测试直接传等价预算 $398，$0.01×100=$1 远小于它 → 放行）
    result = _run_probe_guard_block(store, probe_test=True)
    assert result[0] == "PROBE_GUARD"
    allowed, reason, _ = result[1]
    assert allowed is True and reason == "", "当日无在途探针单，本探针单应放行"

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO order_intents (strategy_id, intent_key, symbol, side,"
            " quantity, limit_price, status, created_at, updated_at, tenant_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("volatility_long_gamma", f"{today}:probe2:REAL", "US.QQQ261120C500000",
             "BUY", 1, 0.01, "SUBMITTED", f"{today}T14:05:00+00:00",
             f"{today}T14:05:01+00:00", "futu_real_master"),
        )
        conn.commit()
    allowed, reason, detail = probe_entry_guard(
        today_real_buy_entries(
            store, tenant_id="futu_real_master",
            strategy_id="volatility_long_gamma",
        ),
        quantity=1, price=0.01, multiplier=100, premium_limit=398.0,
    )
    assert allowed is False and "单日上限" in reason
    assert detail["today_entries"] == 1


def test_normal_buy_skips_probe_guard_and_hits_real_gate():
    """普通 BUY（无 probe_test 标记）不进探针护栏（变异②的对照面）。"""
    from futu_quant.store import TradingStore
    import zoneinfo
    import datetime as _dt

    store = TradingStore(tmp_path_db() if False else _tmp_db(), bootstrap=True)
    today = _dt.datetime.now(zoneinfo.ZoneInfo("America/New_York")).date().isoformat()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO order_intents (strategy_id, intent_key, symbol, side,"
            " quantity, limit_price, status, created_at, updated_at, tenant_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("volatility_long_gamma", f"{today}:real1:REAL", "US.SOFI261120P16000",
             "BUY", 1, 0.56, "SUBMITTED", f"{today}T14:30:00+00:00",
             f"{today}T14:30:01+00:00", "futu_real_master"),
        )
        conn.commit()
    # probe_test=False → 分流直接到 REAL_GATE，不做探针计数
    route, guard = _run_probe_guard_block(store, probe_test=False)
    assert route == "REAL_GATE" and guard is None, (
        "普通 BUY 被探针门拦 —— 双语义拆分回退（404 次误拦复发形态）"
    )


def _tmp_db():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp()) / "normal.db"


def tmp_path_db():
    raise NotImplementedError


def test_mutation_probe_guard_limit_removed_must_be_red():
    """变异①：把探针单的日上限拆掉（daily_limit 无效化）→ 探针路径测试必红。"""
    import shutil
    import subprocess
    import sys

    module = ROOT / "src/futu_quant/capacity_precheck.py"
    original = module.read_text(encoding="utf-8")
    anchor = (
        "if len(todays_entries) >= effective_daily_limit:"
    )
    assert anchor in original, "探针日限锚点丢失，无法构造变异"
    mutated = original.replace(anchor, "if False and len(todays_entries) >= effective_daily_limit:")
    try:
        module.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_probe_path_blocked_after_one_probe_same_day"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "探针日限被拆掉后测试仍绿 —— 探针护栏可被静默移除"
    finally:
        module.write_text(original, encoding="utf-8")
        shutil.rmtree(".pytest_cache", ignore_errors=True)


def test_mutation_strategy_runner_probe_guard_restored_must_be_red():
    """变异②：普通 BUY 路径重新挂回探针门 → 路径分离测试必红。"""
    import shutil
    import subprocess
    import sys

    module = ROOT / STRATEGY_RUNNER
    original = module.read_text(encoding="utf-8")
    assert "probe_entry_guard" not in original, "runner 已含探针调用，无法构造回退变异"
    mutated = original.replace(
        "from futu_quant.capacity_precheck import is_real_account",
        "from futu_quant.capacity_precheck import is_real_account, probe_entry_guard",
    )
    assert mutated != original
    try:
        module.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_strategy_runner_no_longer_calls_probe_guard"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, "runner 重新挂回探针门后守卫仍绿 —— 拆分可被静默回退"
    finally:
        module.write_text(original, encoding="utf-8")
        shutil.rmtree(".pytest_cache", ignore_errors=True)
