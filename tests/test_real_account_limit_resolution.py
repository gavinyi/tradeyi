"""回归：实盘账户级限额的解析口径唯一 —— 账户级优先、全局兜底（Chief 第 ③ 条）。

背景（10-22 实测）：改全局 `REAL_MAX_DAILY_NOTIONAL` 完全无效，
真正生效的是账户级 `real_account_config:<id>.max_daily_notional` —— 同一概念两处配置，
靠各处记忆字段名/回落规则，改 A 生效的是 B。

修复：新增 `resolve_real_account_limits()` 作为**唯一解析口径**，
实盘闸门、下单前置的账户开关、界面三处都读它的结果，不再各自解析。

本文件锁定「账户级覆盖全局 / 缺项回落全局 / 开关语义一致」。
"""
from __future__ import annotations

from dataclasses import replace
import inspect

from futu_quant.config import Settings
from futu_quant.execution import ExecutionManager
from futu_quant.real_trading import resolve_real_account_limits
from futu_quant.store import TradingStore

ACCOUNT = "281756481476169109"


def _settings(monkeypatch, **overrides) -> Settings:
    """构造一份 Settings：先按环境变量加载，再覆盖本次关心的字段。"""
    monkeypatch.setenv("FUTU_TRADING_ENV", "REAL")
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("REAL_ALLOWED_STRATEGIES", "core_satellite,volatility_long_gamma")
    monkeypatch.setenv("REAL_MAX_ORDER_NOTIONAL", "2500")
    monkeypatch.setenv("REAL_MAX_DAILY_NOTIONAL", "5000")
    monkeypatch.setenv("REAL_REQUIRE_MANUAL_CONFIRM", "false")
    loaded = Settings.load(refresh=True)
    return replace(loaded, **overrides) if overrides else loaded


def test_account_level_overrides_global(tmp_path, monkeypatch):
    """账户级存在时必须覆盖全局（10-22 真正生效的那一份）。"""
    store = TradingStore(tmp_path / "override.db")
    store.set_setting(
        f"real_account_config:{ACCOUNT}",
        '{"max_daily_notional": 20000, "max_order_notional": 9999, "enabled": true}',
    )
    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)
    assert limits.max_daily_notional == 20000.0, "账户级未覆盖全局 —— 会重现「改全局无效」"
    assert limits.max_order_notional == 9999.0
    assert limits.config_present is True


def test_missing_keys_fall_back_to_global(tmp_path, monkeypatch):
    """账户级缺项必须回落全局（不得变成 0 或抛异常）。"""
    store = TradingStore(tmp_path / "fallback.db")
    store.set_setting(
        f"real_account_config:{ACCOUNT}",
        '{"allowed_strategies": ["core_satellite"]}',
    )
    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)
    assert limits.max_daily_notional == 5000.0, "缺项未回落全局"
    assert limits.max_order_notional == 2500.0
    assert limits.allowed_strategies == frozenset({"core_satellite"})


def test_no_account_config_uses_global(tmp_path, monkeypatch):
    """完全没有账户级配置时，全部取全局。"""
    store = TradingStore(tmp_path / "global.db")
    limits = resolve_real_account_limits(store, _settings(monkeypatch), "unknown_account")
    assert limits.config_present is False
    assert limits.max_daily_notional == 5000.0
    assert limits.max_order_notional == 2500.0
    assert limits.allowed_strategies == frozenset(
        {"core_satellite", "volatility_long_gamma"}
    )


def test_disabled_account_is_off(tmp_path, monkeypatch):
    """账户开关关闭时 enabled=False，且必须优先于任何限额。"""
    store = TradingStore(tmp_path / "disabled.db")
    store.set_setting(
        f"real_account_config:{ACCOUNT}",
        '{"enabled": false, "max_daily_notional": 999999}',
    )
    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)
    assert limits.enabled is False


def test_malformed_account_config_does_not_crash(tmp_path, monkeypatch):
    """脏配置（非 JSON / 非 dict）必须安全回落全局，不得抛异常。"""
    store = TradingStore(tmp_path / "malformed.db")
    store.set_setting(f"real_account_config:{ACCOUNT}", "{not json")
    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)
    assert limits.max_daily_notional == 5000.0
    store.set_setting(f"real_account_config:{ACCOUNT}", '["a", "b"]')
    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)
    assert limits.max_daily_notional == 5000.0, "非 dict 配置未回落全局"


def test_only_one_place_parses_account_config():
    """铁律：不得再出现第二处自行解析账户级配置的实现。"""
    src = inspect.getsource(ExecutionManager)
    # 下单前置的账户开关必须走共享解析器，而不是自己 json.loads
    assert "resolve_real_account_limits" in src, "下单前置未走共享解析器"
    assert 'real_account_config:' not in src, "下单前置又自行解析了账户级配置 —— 口径会漂移"
    assert "json.loads(raw_acc_cfg)" not in src


def test_resolver_tolerates_partial_settings_double(tmp_path):
    """调用方可能只带部分字段的轻量对象 —— 缺字段必须回落，不得抛异常。"""
    from types import SimpleNamespace

    store = TradingStore(tmp_path / "partial.db")
    store.set_setting(f"real_account_config:{ACCOUNT}", '{"max_daily_notional": 7777}')
    limits = resolve_real_account_limits(store, SimpleNamespace(), ACCOUNT)
    assert limits.max_daily_notional == 7777.0
    assert limits.enabled is True
    assert limits.max_order_notional >= 0.0
