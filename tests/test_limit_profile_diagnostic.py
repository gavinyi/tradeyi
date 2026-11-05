from dataclasses import replace

from futu_quant.config import Settings
from futu_quant.limit_profile import LimitProfile, effective_limit, resolve_limit_chain
from futu_quant.real_trading import check_real_trade
from futu_quant.store import TradingStore


def test_stage1_derives_without_store_or_runtime_side_effects():
    chain = resolve_limit_chain(
        None,
        "futu_paper_sandbox",
        982595.95,
        LimitProfile(daily_entries_ratio=0.12, daily_loss_ratio=0.12),
    )
    assert chain.per_order == chain.batch == 50000.0
    assert chain.daily_entries == 100000.0
    assert chain.daily_loss == 50000.0
    assert chain.clamped == {
        "per_order": "hard_cap:50000",
        "daily_entries": "hard_cap:100000",
        "daily_loss": "hard_cap:50000",
    }


def test_real_profile_keeps_capacity_separate():
    chain = resolve_limit_chain(
        None, "futu_real_master", 6637.64, LimitProfile(capacity_ratio=0.06)
    )
    assert round(chain.per_order, 2) == 398.26
    assert round(chain.capacity_line, 2) == 398.26
    allowed, rejected_by, limit = effective_limit(chain, 615)
    assert not allowed
    assert rejected_by == "per_order"
    assert round(limit, 2) == 398.26


def test_floor_is_explicit_and_missing_equity_fails_closed():
    chain = resolve_limit_chain(None, "futu_real_master", 1000)
    assert chain.per_order == 100.0
    assert chain.daily_entries == 200.0
    assert chain.daily_loss == 200.0
    try:
        resolve_limit_chain(None, "futu_real_master", 0)
    except ValueError as exc:
        assert "equity" in str(exc)
    else:
        raise AssertionError("权益缺失必须拒绝生成诊断值")


def test_real_limit_profile_missing_equity_is_unresolved(monkeypatch, tmp_path):
    """派生层权益缺失不得回落旧金额，必须显式标记未解析。"""
    from types import SimpleNamespace
    import futu_quant.real_trading as real_trading
    import futu_quant.tenant as tenant
    import futu_quant.limit_profile_store as profile_store

    store = TradingStore(tmp_path / "missing-equity.db")
    store.set_setting("real_account_config:281756481476169109", '{"enabled": true}')
    monkeypatch.setattr(
        tenant, "resolve_tenant", lambda *_args, **_kwargs: SimpleNamespace(tenant_id="futu_real_master", trading_env="REAL", account_id="281756481476169109")
    )
    monkeypatch.setattr(profile_store, "load_limit_profile", lambda *_: SimpleNamespace(
        source="derived:limit_profile", profile=LimitProfile()))
    monkeypatch.setattr(store, "equity_history", lambda *, limit=1, env=None: [])
    settings = SimpleNamespace(real_max_order_notional=1000.0, real_max_daily_notional=2500.0,
                               real_allowed_strategies=("core_satellite",),
                               real_require_manual_confirm=False, trading_env="REAL")
    limits = real_trading.resolve_real_account_limits(store, settings, "281756481476169109")
    assert limits.limit_profile_unresolved is True
    assert "equity" in limits.limit_profile_reason


def test_real_limit_profile_stale_equity_is_unresolved(monkeypatch, tmp_path):
    """派生层权益快照超过 10 分钟不得回落旧金额。"""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    import futu_quant.real_trading as real_trading
    import futu_quant.tenant as tenant
    import futu_quant.limit_profile_store as profile_store

    store = TradingStore(tmp_path / "stale-equity.db")
    store.set_setting("real_account_config:281756481476169109", '{"enabled": true}')
    monkeypatch.setattr(
        tenant, "resolve_tenant", lambda *_args, **_kwargs: SimpleNamespace(tenant_id="futu_real_master", trading_env="REAL", account_id="281756481476169109")
    )
    monkeypatch.setattr(profile_store, "load_limit_profile", lambda *_: SimpleNamespace(
        source="derived:limit_profile", profile=LimitProfile()))
    stale_at = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    def equity_history(*, limit=1, env=None):
        # 现行派生路径显式传入 limit 与 tenant 派生的 env；保留参数以钉住调用契约。
        assert limit == 1
        assert env == "REAL_281756481476169109"
        return [{"total_assets": 6637.64, "created_at": stale_at}]

    monkeypatch.setattr(store, "equity_history", equity_history)
    limits = real_trading.resolve_real_account_limits(store, SimpleNamespace(
        real_max_order_notional=1000.0, real_max_daily_notional=2500.0,
        real_allowed_strategies=("core_satellite",), real_require_manual_confirm=False, trading_env="REAL"),
        "281756481476169109")
    assert limits.limit_profile_unresolved is True
    assert "过期" in limits.limit_profile_reason


def test_check_real_trade_rejects_unresolved_profile_with_reason_code(monkeypatch, tmp_path):
    """派生层未解析时 BUY 必须拒绝并返回稳定原因码。"""
    from types import SimpleNamespace
    import futu_quant.real_trading as real_trading

    store = TradingStore(tmp_path / "unresolved-gate.db")
    settings = SimpleNamespace(trading_env="REAL", real_trading_enabled=True,
                               real_account_id="281756481476169109")
    limits = real_trading.RealAccountLimits(
        enabled=True, allowed_strategies=frozenset({"core_satellite"}),
        max_order_notional=1000.0, max_daily_notional=2500.0,
        require_manual_confirm=False, limit_profile_unresolved=True,
        limit_profile_reason="权益快照缺失")
    monkeypatch.setattr(real_trading, "resolve_real_account_limits", lambda *args: limits)
    decision = real_trading.check_real_trade(
        store, settings, "core_satellite", "US.SPY", "BUY", 1, 1.0, {})
    assert decision.allowed is False
    assert decision.details["reason_code"] == "LIMIT_PROFILE_UNRESOLVED"
    assert decision.reason.startswith("LIMIT_PROFILE_UNRESOLVED")


def test_check_real_trade_allows_sell_when_profile_unresolved(monkeypatch, tmp_path):
    """派生层未解析时 SELL 必须保留退出通道，并写专用审计。"""
    from types import SimpleNamespace
    import futu_quant.real_trading as real_trading

    store = TradingStore(tmp_path / "unresolved-sell.db")
    settings = SimpleNamespace(trading_env="REAL", real_trading_enabled=True,
                               real_account_id="281756481476169109")
    limits = real_trading.RealAccountLimits(
        enabled=True, allowed_strategies=frozenset({"core_satellite"}),
        max_order_notional=1000.0, max_daily_notional=2500.0,
        require_manual_confirm=False, limit_profile_unresolved=True,
        limit_profile_reason="权益快照缺失")
    monkeypatch.setattr(real_trading, "resolve_real_account_limits", lambda *args: limits)
    monkeypatch.setattr(real_trading, "reduction_notional_limits", lambda *args: (150000.0, 500000.0))
    monkeypatch.setattr(real_trading, "session_status", lambda *args, **kwargs: (True, "", {}))
    monkeypatch.setattr(real_trading, "submitted_real_reduction_notional_since", lambda *args, **kwargs: 0.0)
    decision = real_trading.check_real_trade(
        store, settings, "core_satellite", "US.SPY", "SELL", 1, 1.0, {})
    assert decision.allowed is True
    assert store.audit_events(limit=1)[0]["event_type"] == "LIMIT_PROFILE_UNRESOLVED_SELL_ALLOWED"


def test_diagnostic_rejects_by_tightest_layer():
    profile = LimitProfile(per_order_ratio=0.10, daily_entries_ratio=0.02)
    chain = resolve_limit_chain(None, "futu_paper_sandbox", 10000, profile)
    allowed, rejected_by, limit = effective_limit(chain, 250)
    assert not allowed and rejected_by == "daily_entries" and limit == 200.0
