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
