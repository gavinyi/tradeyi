"""``decision_if_changed`` / ``decision_logs`` 的租户维度守卫。

守的是一件**行为**：雷达按租户扇出后，同一个 ``strategy_id + symbol`` 会被多个
租户各判一次，两方都必须留痕、互不压制；而**同一租户内**仍要去重。

改前实测的失效形态：``_DECISION_CACHE`` 键是 ``(strategy_id, symbol)``，进程内
共享 —— 租户 A 的决策会在 ``repeat_after_seconds``（默认 900 秒）内把租户 B 对
同一标的的留痕吞掉，``decision_logs`` 里只剩一方，**静默丢审计**。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from futu_quant import volatility_scanner as scanner  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402

REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"


@pytest.fixture(autouse=True)
def _clear_decision_cache():
    """去重缓存是**类属性**（进程内共享），用例之间必须清干净。"""
    TradingStore._DECISION_CACHE.clear()
    yield
    TradingStore._DECISION_CACHE.clear()


def _decision_kwargs() -> dict:
    return {
        "strategy_id": "volatility_scanner",
        "symbol": "US.META",
        "decision": "HOLD",
        "reason": "未达到动态波动监测条件",
    }


def test_two_tenants_same_symbol_both_recorded(tmp_path):
    """租户 A 的决策不得压住租户 B 的留痕。"""
    store = TradingStore(tmp_path / "t.db")

    assert store.decision_if_changed(**_decision_kwargs(), tenant_id=REAL) is True
    assert store.decision_if_changed(**_decision_kwargs(), tenant_id=PAPER) is True, (
        "租户 B 的决策被租户 A 的进程内缓存压住 —— 这正是扇出后静默丢留痕的形态"
    )

    tenants = {row["tenant_id"] for row in store.decisions(limit=10)}
    assert tenants == {REAL, PAPER}, f"两个租户都必须留痕，实际 {tenants}"


def test_same_tenant_still_deduplicates(tmp_path):
    """加了租户维度**不能把去重弄丢** —— 同租户同决策仍在窗口内跳过。"""
    store = TradingStore(tmp_path / "t.db")

    assert store.decision_if_changed(**_decision_kwargs(), tenant_id=REAL) is True
    assert store.decision_if_changed(**_decision_kwargs(), tenant_id=REAL) is False
    assert len(store.decisions(limit=10)) == 1


def test_fan_out_stamps_tenant_on_decision(tmp_path, monkeypatch):
    """扇出层必须把租户**传下去** —— 只改 store 不改调用点等于没修。"""
    store = TradingStore(tmp_path / "t.db")
    monkeypatch.setattr(scanner, "load_volatility_campaigns", lambda tenant_id: [])
    market = scanner.MarketScan(
        symbols=("US.META",),
        market_move=0.0,
        results={
            "US.META": scanner.MarketSymbolResult(
                symbol="US.META",
                owner=None,
                bars=None,
                profile={},
                regime={"active": False, "reason": "未达到动态波动监测条件"},
                relative_strength=0.0,
                market_independent=False,
                row={"symbol": "US.META", "active": False},
            ),
        },
        rows=[],
    )

    for tenant in (REAL, PAPER):
        profile = SimpleNamespace(tenant_id=tenant, trading_env="REAL")
        broker = SimpleNamespace(tenant_id=tenant, account_id="test-account")
        scanner.fan_out_to_tenant(store, broker, profile, market, ("US.META",))

    tenants = {row["tenant_id"] for row in store.decisions(limit=10)}
    assert tenants == {REAL, PAPER}, (
        f"扇出层的决策必须有本租户 tenant_id，实际 {tenants} —— "
        "调用点没传租户时这里会只剩空串"
    )


def test_missing_tenant_id_records_warning_event(tmp_path):
    """P1-7 契约：解析不到租户时不许静默落空，必须记录 WARNING 事件说明调用点。"""
    store = TradingStore(tmp_path / "t.db")

    # 1. 显式传租户：正常落库，且绝不产生缺失警告
    store.decision("test_strat", "US.AAPL", "HOLD", "正常决策", tenant_id=REAL)
    warnings = [e for e in store.operational_events() if e["event_type"] == "DECISION_MISSING_TENANT_ID"]
    assert len(warnings) == 0, "传了 tenant_id 不得触发缺失警告"
    last_log = store.decisions(limit=1)[0]
    assert last_log["tenant_id"] == REAL

    # 2. 未传租户：落库空串，同时写一条 WARNING 事件记录调用点
    store.decision("test_strat", "US.AAPL", "HOLD", "无租户决策")
    warnings = [e for e in store.operational_events() if e["event_type"] == "DECISION_MISSING_TENANT_ID"]
    assert len(warnings) == 1, "未传 tenant_id 必须生成一条 WARNING 事件"
    assert warnings[0]["status"] == "WARNING"
    import json
    payload = json.loads(warnings[0]["payload_json"])
    assert payload["strategy_id"] == "test_strat"
    assert payload["symbol"] == "US.AAPL"
    assert "caller" in payload
    assert "test_decision_tenant_scope.py" in payload["caller"]


def test_decision_behavior_unchanged_with_tenant_id(tmp_path):
    """P1-7 验收：同一决策补 tenant_id 前后行为严格一致（只增加审计维度，不改业务逻辑）。"""
    store = TradingStore(tmp_path / "t.db")

    # 未带租户 vs 带租户
    store.decision("core_satellite", "US.SPY", "HOLD", "维持仓位", payload={"target": 0.5}, tenant_id=None)
    store.decision("core_satellite", "US.SPY", "HOLD", "维持仓位", payload={"target": 0.5}, tenant_id=REAL)

    logs = store.decisions(limit=10)
    assert len(logs) == 2
    assert logs[0]["decision"] == logs[1]["decision"] == "HOLD"
    assert logs[0]["reason"] == logs[1]["reason"] == "维持仓位"
    assert logs[0]["tenant_id"] == REAL
    assert logs[1]["tenant_id"] == ""


def test_worker_run_campaign_records_tenant_id(tmp_path, monkeypatch):
    """P1-7 验收：Worker 执行 run_campaign 时，决策日志准确落入 campaign["tenant_id"]。"""
    import pandas as pd
    import scripts.run_gamma_strategy as rgs
    from futu_quant.execution import ExecutionManager

    store = TradingStore(tmp_path / "t.db")

    code = "US.META261030P660000"
    # 数量 0：走 run_campaign 的 `quantity <= 0` 早退分支（写一条 HOLD 决策即返回），
    # 本用例只验「决策留痕带租户」，不承载持仓退出链路的完整撮合。
    positions = pd.DataFrame([{"code": code, "qty": 0, "cost_price": 2.0}])

    class FakeBroker:
        def positions(self, refresh=False):
            return positions

        def orders(self, *args, **kwargs):
            return pd.DataFrame()

        def snapshots(self, symbols):
            """批 B 起入场先取期权/正股快照判盘口活性，桩要按请求的代码逐条回。"""
            stamp = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M:%S")
            rows = []
            for symbol in symbols:
                if symbol == code:
                    rows.append({"code": symbol, "bid_price": 1.95, "ask_price": 2.05,
                                 "last_price": 2.0, "update_time": stamp})
                else:
                    rows.append({"code": symbol, "bid_price": 659.90, "ask_price": 660.10,
                                 "last_price": 660.0, "update_time": stamp})
            return pd.DataFrame(rows)

        def snapshots_tolerant(self, symbols, **kwargs):
            return self.snapshots(symbols)

        def candles(self, symbol, count=120):
            bars = max(1, int(count or 120))
            closes = [600.0 + index for index in range(bars)]
            return pd.DataFrame({
                "code": symbol,
                "time_key": pd.date_range("2026-07-01", periods=bars, freq="D"),
                "open": closes, "close": closes,
                "high": closes, "low": closes, "volume": 1000,
            })

    executor = ExecutionManager(FakeBroker(), store=store)

    monkeypatch.setattr(rgs, "diagnose_campaign", lambda campaign, events: ["财报时间未人工确认"])
    monkeypatch.setattr(rgs, "hard_campaign_issues", lambda campaign, issues: list(issues))

    campaign = {
        "code": code,
        "trigger_mode": "VOLATILITY",
        "underlying": "META",
        "option_type": "PUT",
        "legs": [{"code": code, "side": "BUY", "ratio": 1}],
        "tenant_id": REAL,
        "closed": True,  # 触发 CLOSED 决策
    }
    rgs.run_campaign(FakeBroker(), executor, campaign, {})

    decisions = store.decisions(limit=10)
    assert len(decisions) >= 1
    assert decisions[0]["tenant_id"] == REAL, f"Worker 决策未带入 campaign 租户：{decisions[0]}"
    # 并且不应该产生缺失租户的警告
    warnings = [e for e in store.operational_events() if e["event_type"] == "DECISION_MISSING_TENANT_ID"]
    assert len(warnings) == 0, f"Worker 决策触发了未传租户告警：{warnings}"


def test_missing_tenant_warning_is_throttled_per_process(tmp_path):
    """热路径保护（Chief 复核追加）：同一调用点缺租户只报一次。

    `decision()` 是雷达热路径（每轮每候选一条）。若缺 tenant_id 就每次都写
    `operational_events`，事件表会被瞬间写爆 —— 而「这处调用点一直缺租户」这件事
    只需要报一次。变异：去掉 `_MISSING_TENANT_WARNED` 闸门 → 本用例红（20 条告警）。
    """
    store = TradingStore(tmp_path / "t.db")
    TradingStore._MISSING_TENANT_WARNED.clear()

    for _ in range(20):
        store.decision("hot_path_strat", "US.AAPL", "HOLD", "热路径决策")
    warnings = [
        e for e in store.operational_events()
        if e["event_type"] == "DECISION_MISSING_TENANT_ID"
    ]
    assert len(warnings) == 1, (
        f"同一 (strategy, symbol) 缺租户应只报一次，实际 {len(warnings)} 条 —— 事件表会被写爆"
    )

    # 不能一刀切静音：换一个标的仍要各报一次
    store.decision("hot_path_strat", "US.NVDA", "HOLD", "另一个标的")
    warnings = [
        e for e in store.operational_events()
        if e["event_type"] == "DECISION_MISSING_TENANT_ID"
    ]
    assert len(warnings) == 2, "不同标的的缺失必须分别可见，不能连坐静音"


