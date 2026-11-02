"""守卫：账户资产币种不是 USD 时，策略压力损失预算必须 **fail-closed 拒开仓**。

事故背景（2026-11-02 实盘复盘）：
- `accinfo_query` 不传 `currency` 时返回**账户本位币**（该实盘账户为 HKD），
  而 `positions()` 的 market_val 是美元 —— 混算让「账户资产」放大 7.85 倍；
- 「压力损失预算」按权益百分比计算，于是被静默放宽 7.85 倍：10-29/10-30 实盘
  正是靠这个虚高额度在超配开仓（$730–$1,116 的单都过了）；
- `broker.py` 已改显式 `currency="USD"`，但**返回值是否真被采纳没人校验**，
  `broker.py:470` 只打 warning。本文件锁住「校验 + 拒开仓」这条。

判据：
1. 币种为 HKD 等非 USD → `strategy_budget_allowed` 必须返回 False，理由点名币种；
2. 缓存快照（live 调用失败时回落）同样要校验，不能走缓存绕过；
3. USD / N/A / 无币种列 → 维持原有判定，不新增误拒。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from futu_quant.portfolio_allocator import (
    AccountCurrencyMismatch,
    account_currency_status,
    strategy_budget_allowed,
    strategy_budget_usage,
)
from futu_quant.store import TradingStore

STRATEGY = "volatility_long_gamma"
SYMBOL = "US.AMZN261113C250000"


class _Broker:
    """最小 broker：只提供 account_info / positions / 若干快照字段。"""

    account_id = "281756481476169109"
    trading_env = "REAL"

    def __init__(self, account_frame: pd.DataFrame, positions_frame: pd.DataFrame | None = None):
        self._account = account_frame
        self._positions = positions_frame if positions_frame is not None else pd.DataFrame()

    def account_info(self, refresh: bool = False):
        return self._account

    def positions(self, refresh: bool = False):
        return self._positions

    def snapshots(self, symbols):
        return pd.DataFrame()

    def orders(self, refresh: bool = False):
        return pd.DataFrame()


def _account(currency: str, total_assets: float = 6643.0) -> pd.DataFrame:
    return pd.DataFrame([{"currency": currency, "total_assets": total_assets}])


def test_currency_status_flags_non_usd():
    ok, actual = account_currency_status(_account("HKD"))
    assert ok is False
    assert actual == "HKD"


def test_currency_status_accepts_usd_and_blank():
    assert account_currency_status(_account("USD")) == (True, "USD")
    assert account_currency_status(_account("N/A")) == (True, "N/A")
    assert account_currency_status(pd.DataFrame([{"total_assets": 1.0}])) == (True, "")


def test_usage_raises_on_non_usd_account(tmp_path):
    store = TradingStore(tmp_path / "usage.db")

    with pytest.raises(AccountCurrencyMismatch):
        strategy_budget_usage(store, _Broker(_account("HKD")))


def test_budget_allowed_rejects_open_on_non_usd(tmp_path):
    """核心判据：币种不对时开仓必须被拒，且理由里点名币种。"""
    store = TradingStore(tmp_path / "reject.db")

    allowed, reason, details = strategy_budget_allowed(
        store, _Broker(_account("HKD", total_assets=51_990.85)),
        STRATEGY, SYMBOL, "BUY", 1, 6.55, 100.0,
    )

    assert allowed is False
    assert "HKD" in reason
    assert details.get("currency_mismatch") is True


def test_budget_allowed_rejects_from_cached_snapshot_too(tmp_path):
    """live 调用失败时回落缓存快照 —— 缓存里是 HKD 也必须拒，不能靠缓存绕过。"""
    store = TradingStore(tmp_path / "cached.db")
    store.save_broker_snapshot(
        "account_info", [{"currency": "HKD", "total_assets": 51_990.85}],
    )
    store.save_broker_snapshot("positions", [])

    class _BrokenBroker(_Broker):
        def account_info(self, refresh: bool = False):
            raise RuntimeError("OpenD 断线")

        def positions(self, refresh: bool = False):
            raise RuntimeError("OpenD 断线")

    allowed, reason, details = strategy_budget_allowed(
        store, _BrokenBroker(_account("USD")), STRATEGY, SYMBOL, "BUY", 1, 6.55, 100.0,
    )

    assert allowed is False
    assert "HKD" in reason
    assert details.get("currency_mismatch") is True


def test_budget_allowed_still_works_for_usd_account(tmp_path):
    """USD 账户不得被新守卫误拒（原有预算判定照常生效）。"""
    store = TradingStore(tmp_path / "usd.db")

    allowed, reason, _ = strategy_budget_allowed(
        store, _Broker(_account("USD", total_assets=6_643.0)),
        STRATEGY, SYMBOL, "BUY", 1, 0.5, 100.0,
    )

    assert isinstance(allowed, bool)
    assert "HKD" not in reason


def test_sell_is_never_blocked_by_currency_guard(tmp_path):
    """减仓/平仓不被这条守卫拦（只拦新增风险），否则会堵死退出通道。"""
    store = TradingStore(tmp_path / "sell.db")

    allowed, reason, _ = strategy_budget_allowed(
        store, _Broker(_account("HKD")), STRATEGY, SYMBOL, "SELL", 1, 6.55, 100.0,
    )

    assert allowed is True


def test_broker_warns_but_allocator_is_the_hard_gate():
    """`broker.py` 只 warning（避免打崩服务循环），硬门禁在 allocator —— 二者分工要写死在守卫里。"""
    root = Path(__file__).resolve().parents[1]
    broker_src = (root / "src" / "futu_quant" / "broker.py").read_text(encoding="utf-8")
    allocator_src = (root / "src" / "futu_quant" / "portfolio_allocator.py").read_text(encoding="utf-8")

    assert "logger.warning" in broker_src
    assert "AccountCurrencyMismatch" in allocator_src
    assert "currency_mismatch" in allocator_src
