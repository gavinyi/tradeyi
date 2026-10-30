"""实盘闸口总览测试（trading_gate_overview）。

目标：界面显示的「能不能下单、被哪层挡住」必须与下单路径 check_real_trade 同源。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from futu_quant.real_trading import (
    confirmation_code,
    enable_session,
    trading_gate_overview,
)
from futu_quant.store import TradingStore


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        trading_env="REAL",
        real_trading_enabled=True,
        real_account_id="281756481476169109",
        real_max_order_notional=2500.0,
        real_max_daily_notional=3000.0,
        real_allowed_strategies=("core_satellite", "volatility_long_gamma"),
        real_require_manual_confirm=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _store(tmp_path: Path) -> TradingStore:
    return TradingStore(str(tmp_path / "gate.db"))


def _account_config(enabled: bool, require_confirm: bool = False) -> str:
    return (
        '{"enabled": %s, "allowed_strategies": ["core_satellite", "volatility_long_gamma"],'
        ' "max_order_notional": 2500.0, "max_daily_notional": 3000.0,'
        ' "require_manual_confirm": %s}'
        % ("true" if enabled else "false", "true" if require_confirm else "false")
    )


def test_all_gates_pass(tmp_path):
    store = _store(tmp_path)
    store.set_setting("real_account_config:281756481476169109", _account_config(True))
    store.set_setting("strategy_enabled:futu_real_master:core_satellite", "true")
    overview = trading_gate_overview(
        store, _settings(), tenant_id="futu_real_master", strategies=("core_satellite",),
    )
    assert overview.allowed is True
    assert overview.blocking == ()
    assert [layer.key for layer in overview.layers] == [
        "env", "master_switch", "tenant", "account", "confirm_session",
    ]
    assert overview.strategies == (("core_satellite", True),)
    assert "可下单" in overview.verdict


def test_account_switch_off_blocks_and_is_named(tmp_path):
    store = _store(tmp_path)
    store.set_setting("real_account_config:281756481476169109", _account_config(False))
    overview = trading_gate_overview(store, _settings(), tenant_id="futu_real_master")
    assert overview.allowed is False
    assert [layer.key for layer in overview.blocking] == ["account"]
    # 结论里必须点名是哪一层挡的，否则用户还得自己逐页找
    assert "账户开关" in overview.verdict


def test_master_switch_off_blocks(tmp_path):
    store = _store(tmp_path)
    store.set_setting("real_account_config:281756481476169109", _account_config(True))
    overview = trading_gate_overview(
        store, _settings(real_trading_enabled=False), tenant_id="futu_real_master",
    )
    assert overview.allowed is False
    assert "master_switch" in [layer.key for layer in overview.blocking]


def test_inactive_tenant_blocks(tmp_path):
    store = _store(tmp_path)
    store.set_setting("real_account_config:281756481476169109", _account_config(True))
    store.set_setting("tenant_active:futu_real_master", "false")
    overview = trading_gate_overview(store, _settings(), tenant_id="futu_real_master")
    assert overview.allowed is False
    assert "tenant" in [layer.key for layer in overview.blocking]


def test_manual_confirm_off_means_session_not_required(tmp_path):
    """关闭人工确认时，session 为空也必须判「通过」——否则界面会天天报拦截。"""
    store = _store(tmp_path)
    store.set_setting("real_account_config:281756481476169109", _account_config(True))
    overview = trading_gate_overview(store, _settings(), tenant_id="futu_real_master")
    session_layer = next(layer for layer in overview.layers if layer.key == "confirm_session")
    assert session_layer.ok is True
    assert "未启用" in session_layer.detail


def test_manual_confirm_on_requires_valid_session(tmp_path):
    store = _store(tmp_path)
    settings = _settings(real_require_manual_confirm=True)
    store.set_setting(
        "real_account_config:281756481476169109", _account_config(True, require_confirm=True),
    )
    overview = trading_gate_overview(store, settings, tenant_id="futu_real_master")
    assert overview.allowed is False
    assert "confirm_session" in [layer.key for layer in overview.blocking]

    code = confirmation_code(settings)
    ok, message = enable_session(store, settings, code, minutes=30)
    assert ok, message
    overview = trading_gate_overview(store, settings, tenant_id="futu_real_master")
    assert overview.allowed is True


def test_expired_session_blocks(tmp_path):
    store = _store(tmp_path)
    settings = _settings(real_require_manual_confirm=True)
    store.set_setting(
        "real_account_config:281756481476169109", _account_config(True, require_confirm=True),
    )
    code = confirmation_code(settings)
    assert enable_session(store, settings, code, minutes=30)[0] is True
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    overview = trading_gate_overview(store, settings, tenant_id="futu_real_master", now=future)
    assert overview.allowed is False
    session_layer = next(layer for layer in overview.layers if layer.key == "confirm_session")
    assert "过期" in session_layer.detail
