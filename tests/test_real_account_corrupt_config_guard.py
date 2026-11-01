"""实盘账户级配置「坏键」fail-closed 守卫（2026-11-01 用户拍板口径）。

背景：`real_account_config:<id>` 一旦被写成非法 JSON / 非法字段，旧实现静默按空
配置处理并回落 `enabled=True` —— 这是 **fail-open**（配置坏了反而放行真金下单）。

拍板口径（不可协商）：
1. 键不存在 / 空串 → 按「未配置」走全局兜底，**不是**故障；
2. 键存在但损坏 → **只拦 BUY**；SELL 继续走持仓 / 减仓额度 / 对账等既有校验，不开旁路；
3. 强告警：进不可静音清单，且必须真实投递；
4. 变异必红：把 `enabled=False` 改回 True、去掉告警、把坏键当空串，对应用例必须转红。
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from futu_quant.alerts import ALERT_NEVER_MUTE, AlertManager, is_never_mutable
from futu_quant.config import Settings
from futu_quant.execution import ExecutionManager
from futu_quant.real_trading import (
    RealAccountLimits,
    check_real_trade,
    resolve_real_account_limits,
)
from futu_quant.store import TradingStore

# 判定链测试刻意避开 PROD_ACCOUNT_STRATEGY_MAP 里的两个真实账户，聚焦坏键语义本身
ACCOUNT = "999001"
STRATEGY_ID = "core_satellite"
KEY = f"real_account_config:{ACCOUNT}"

# submit_limit 前置门禁需要 ExecutionManager 能解析出注册租户，必须用真实账户；
# 注意生产库键不存在，本文件全部在 tmp_path 的独立库上跑。
REAL_ACCOUNT = "281756481476169109"
REAL_TENANT = "futu_real_master"
REAL_KEY = f"real_account_config:{REAL_ACCOUNT}"


def _settings(monkeypatch, **overrides) -> Settings:
    monkeypatch.setenv("FUTU_TRADING_ENV", "REAL")
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("REAL_ALLOWED_STRATEGIES", "core_satellite,volatility_long_gamma")
    monkeypatch.setenv("REAL_MAX_ORDER_NOTIONAL", "2500")
    monkeypatch.setenv("REAL_MAX_DAILY_NOTIONAL", "5000")
    monkeypatch.setenv("REAL_REQUIRE_MANUAL_CONFIRM", "false")
    loaded = Settings.load(refresh=True)
    return replace(loaded, **overrides) if overrides else loaded


class FakeRealBroker:
    """最小实盘 broker：只提供 `submit_limit` 前置账户门禁所需字段。"""

    def __init__(self, account_id: str = REAL_ACCOUNT):
        self.tenant_id = "futu_real_master"
        self.trading_env = "REAL"
        self.account_id = account_id
        self.settings = type("Settings", (), {
            "trading_env": "REAL",
            "real_trading_enabled": True,
            "real_account_id": account_id,
            "real_allowed_strategies": (STRATEGY_ID,),
            "real_max_order_notional": 2500.0,
            "real_max_daily_notional": 5000.0,
            "real_require_manual_confirm": False,
        })()
        self.placed_orders: list[dict] = []

    def place_limit_order(self, symbol, side, quantity, price, **kwargs):
        order = {"symbol": symbol, "side": side, "quantity": quantity, "price": price}
        self.placed_orders.append(order)
        return pd.DataFrame([{"order_id": f"ord_{len(self.placed_orders)}"}])

    def positions(self, refresh=False):
        return pd.DataFrame()

    def orders(self, refresh=False):
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# 1. 解析层：坏键 / 未配置 / 合法 三种情形必须分开
# ---------------------------------------------------------------------------


def test_corrupt_json_is_flagged_and_disabled(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "c1.db")
    store.set_setting(KEY, "{not json")

    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)

    assert limits.config_corrupt is True, "非法 JSON 未被判为坏键 —— 这就是 fail-open 的入口"
    assert limits.enabled is False, "坏键必须返回 enabled=False，否则 submit_limit 前置门禁不会拦"
    assert "JSON" in limits.corrupt_reason
    assert limits.config_present is True


def test_non_dict_json_is_corrupt(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "c2.db")
    store.set_setting(KEY, '["a", "b"]')

    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)

    assert limits.config_corrupt is True
    assert "JSON 对象" in limits.corrupt_reason


def test_illegal_numeric_field_is_corrupt(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "c3.db")
    store.set_setting(KEY, '{"max_order_notional": "abc"}')

    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)

    assert limits.config_corrupt is True
    assert "max_order_notional" in limits.corrupt_reason
    # 数值本身仍回落全局，避免坏字段把限额变成 0（那等于禁止一切交易）
    assert limits.max_order_notional == 2500.0


def test_empty_string_is_unconfigured_not_corrupt(tmp_path, monkeypatch):
    """Reviewer 第 3 条：本仓有大量空串占位键，空串是「未配置」，不是坏键。"""
    store = TradingStore(tmp_path / "c4.db")
    store.set_setting(KEY, "")

    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)

    assert limits.config_corrupt is False
    assert limits.config_present is False
    assert limits.enabled is True
    assert limits.max_daily_notional == 5000.0


def test_valid_config_is_not_corrupt(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "c5.db")
    store.set_setting(
        KEY, '{"enabled": true, "max_order_notional": 999, "max_daily_notional": 888}',
    )

    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)

    assert limits.config_corrupt is False
    assert limits.enabled is True
    assert limits.max_order_notional == 999.0
    assert limits.max_daily_notional == 888.0


# ---------------------------------------------------------------------------
# 2. 判定链：BUY 拦、SELL 放行到既有校验
# ---------------------------------------------------------------------------


def test_buy_blocked_when_account_config_corrupt(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "b1.db")
    store.set_setting(KEY, "{not json")

    decision = check_real_trade(
        store, _settings(monkeypatch), STRATEGY_ID, "US.SPY", "BUY", 1, 100.0, {},
    )

    assert decision.allowed is False
    assert "账户级配置损坏" in decision.reason
    assert decision.details.get("config_corrupt") is True


def test_sell_passes_corrupt_gate_but_still_checked(tmp_path, monkeypatch):
    """SELL 不因坏键被拦 —— 但必须证明它继续走既有校验链。"""
    store = TradingStore(tmp_path / "b2.db")
    store.set_setting(KEY, "{not json")

    decision = check_real_trade(
        store, _settings(monkeypatch), STRATEGY_ID, "US.SPY", "SELL", 1, 100.0, {},
    )

    assert decision.allowed is True, "SELL 被坏键拦下了 —— 平仓通道被账户配置损坏堵死"
    assert "配置损坏" not in decision.reason


def test_sell_still_blocked_by_reduction_cap_when_corrupt(tmp_path, monkeypatch):
    """Reviewer 第 6 条：证明「没被坏键拦」不等于「没有其他校验」。

    这里把减仓额度压到 $50（喂的是真实判定链里 `reduction_notional_limits` 的返回值），
    $100 的 SELL 必须仍被这段既有校验拦下 —— 说明坏键没有开旁路。
    """
    store = TradingStore(tmp_path / "b3.db")
    store.set_setting(KEY, "{not json")
    monkeypatch.setattr(
        "futu_quant.real_trading.reduction_notional_limits", lambda *a, **k: (50.0, 50.0),
    )

    decision = check_real_trade(
        store, _settings(monkeypatch), STRATEGY_ID, "US.SPY", "SELL", 1, 100.0, {},
    )

    assert decision.allowed is False, "坏键 SELL 绕过了减仓额度校验 —— 出现旁路"
    assert "减仓上限" in decision.reason


# ---------------------------------------------------------------------------
# 3. submit_limit 前置门禁（enabled=False 的唯一承重点，变异必红）
# ---------------------------------------------------------------------------


def test_submit_limit_blocks_buy_and_audits_when_corrupt(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "s1.db")
    store.set_setting(REAL_KEY, "{not json")
    broker = FakeRealBroker()
    executor = ExecutionManager(broker, store=store, tenant=REAL_TENANT)

    res = executor.submit_limit(STRATEGY_ID, "corrupt-buy-1", "US.SPY", "BUY", 1, 100.0)

    assert res.status == "REJECTED"
    assert "账户级配置损坏" in res.message
    assert broker.placed_orders == []
    assert any(
        e["event_type"] == "REAL_ACCOUNT_CONFIG_CORRUPT" for e in store.audit_events()
    ), "坏键拒单未留审计 —— 事后无法解释「为什么没下单」"


def test_submit_limit_does_not_block_sell_when_corrupt(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "s2.db")
    store.set_setting(REAL_KEY, "{not json")
    broker = FakeRealBroker()
    executor = ExecutionManager(broker, store=store, tenant=REAL_TENANT)

    res = executor.submit_limit(STRATEGY_ID, "corrupt-sell-1", "US.SPY", "SELL", 1, 100.0)

    assert "账户级配置损坏" not in (res.message or ""), "SELL 被坏键前置门禁拦下了 —— 平仓旁路被堵"
    assert not any(
        e["event_type"] == "REAL_ACCOUNT_CONFIG_CORRUPT" for e in store.audit_events()
    ), "SELL 不该写坏键拒单审计"
    assert any(
        e["event_type"] == "REAL_ACCOUNT_CONFIG_CORRUPT_SELL_ALLOWED"
        for e in store.audit_events()
    ), "SELL 放行未留审计 —— 事后无法解释「为什么这笔没被拦」"


# ---------------------------------------------------------------------------
# 4. 告警：不可静音 + 真的被调用（变异必红）
# ---------------------------------------------------------------------------


def test_corrupt_event_is_never_mutable():
    assert "REAL_ACCOUNT_CONFIG_CORRUPT" in ALERT_NEVER_MUTE
    assert is_never_mutable("REAL_ACCOUNT_CONFIG_CORRUPT") is True


def test_corrupt_emits_alert(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "a1.db")
    store.set_setting(KEY, "{not json")
    sent: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        AlertManager, "send",
        lambda self, severity, event_type, message, incident_key=None: sent.append(
            (severity, event_type, message, incident_key)
        ),
    )

    check_real_trade(store, _settings(monkeypatch), STRATEGY_ID, "US.SPY", "BUY", 1, 100.0, {})

    assert [e for _, e, _, _ in sent] == ["REAL_ACCOUNT_CONFIG_CORRUPT"]
    assert sent[0][0] == "ERROR"


def test_corrupt_alert_uses_stable_incident_key(tmp_path, monkeypatch):
    """Reviewer 第 1 条：不带稳定 incident_key 会每笔尝试新开一条 incident + 一张飞书卡。"""
    store = TradingStore(tmp_path / "a2.db")
    store.set_setting(KEY, "{not json")
    keys: list[str] = []
    monkeypatch.setattr(
        AlertManager, "send",
        lambda self, severity, event_type, message, incident_key=None: keys.append(
            incident_key
        ),
    )

    check_real_trade(store, _settings(monkeypatch), STRATEGY_ID, "US.SPY", "BUY", 1, 100.0, {})
    check_real_trade(store, _settings(monkeypatch), STRATEGY_ID, "US.SPY", "BUY", 1, 100.0, {})

    assert keys == [f"REAL_ACCOUNT_CONFIG_CORRUPT:{ACCOUNT}"] * 2


def test_corrupt_alert_dedupes_into_single_incident(tmp_path, monkeypatch):
    """稳定 incident_key + AlertManager 去重窗口 ⇒ 同一坏键只留一条 incident。"""
    store = TradingStore(tmp_path / "a3.db")
    store.set_setting(KEY, "{not json")
    settings = _settings(monkeypatch)

    for _ in range(3):
        check_real_trade(store, settings, STRATEGY_ID, "US.SPY", "BUY", 1, 100.0, {})

    rows = [r for r in store.incidents("OPEN") if "REAL_ACCOUNT_CONFIG_CORRUPT" in str(r)]
    assert len(rows) == 1, f"同一坏键被开成了 {len(rows)} 条 incident —— 会刷屏"


def test_sell_does_not_alert_when_corrupt(tmp_path, monkeypatch):
    """Reviewer 第 2 条：SELL 只落审计，不推送（否则坏键期每笔减仓都会弹卡）。"""
    store = TradingStore(tmp_path / "a4.db")
    store.set_setting(KEY, "{not json")
    sent: list[str] = []
    monkeypatch.setattr(
        AlertManager, "send",
        lambda self, severity, event_type, message, incident_key=None: sent.append(event_type),
    )

    decision = check_real_trade(
        store, _settings(monkeypatch), STRATEGY_ID, "US.SPY", "SELL", 1, 100.0, {},
    )

    assert decision.allowed is True
    assert sent == [], "坏键期间的减仓仍然推送了告警 —— 会刷屏"
    assert any(
        e["event_type"] == "REAL_ACCOUNT_CONFIG_CORRUPT_SELL_ALLOWED"
        for e in store.audit_events()
    ), "SELL 放行必须留审计，否则「为什么这笔没被拦」无据可查"


def test_corrupt_uses_global_limits_so_sell_not_deadlocked(tmp_path, monkeypatch):
    """Reviewer 第 3 条显式约定：坏键时数值回落全局，绝不置 0。

    一旦有人把它改成 0，SELL 会因「单笔减仓超过 0」被自己的坏配置锁死 —— 本用例转红。
    """
    store = TradingStore(tmp_path / "a5.db")
    store.set_setting(KEY, '{"max_order_notional": "abc"}')

    limits = resolve_real_account_limits(store, _settings(monkeypatch), ACCOUNT)

    assert limits.max_order_notional == 2500.0, "坏键把单笔限额变成 0 —— 减仓通道会被锁死"
    assert limits.max_daily_notional == 5000.0
    decision = check_real_trade(
        store, _settings(monkeypatch), STRATEGY_ID, "US.SPY", "SELL", 1, 100.0, {},
    )
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# 5. 变异必红（Reviewer 第 5 条）
# ---------------------------------------------------------------------------


def test_mutation_enabled_true_would_leak_buy(tmp_path, monkeypatch):
    """变异：把坏键的 enabled 由 False 改回 True，submit_limit 前置门禁即失效。"""
    store = TradingStore(tmp_path / "m1.db")
    store.set_setting(KEY, "{not json")
    leaked = RealAccountLimits(
        enabled=True, allowed_strategies=frozenset({STRATEGY_ID}),
        max_order_notional=2500.0, max_daily_notional=5000.0,
        require_manual_confirm=False, config_present=True,
        config_corrupt=True, corrupt_reason="mutated",
    )
    monkeypatch.setattr(
        "futu_quant.execution.resolve_real_account_limits", lambda *a, **k: leaked,
    )
    broker = FakeRealBroker()
    executor = ExecutionManager(broker, store=store, tenant=REAL_TENANT)

    res = executor.submit_limit(STRATEGY_ID, "mut-buy-1", "US.SPY", "BUY", 1, 100.0)

    # 该变异下不再被坏键拦 —— 证明上面那条守卫确实依赖 enabled=False 这一行
    assert "账户级配置损坏" not in (res.message or "")


def test_mutation_treating_corrupt_as_unconfigured_would_leak_buy(tmp_path, monkeypatch):
    """变异：把坏键当空串（走全局兜底），BUY 守卫用例必须能感知到。"""
    store = TradingStore(tmp_path / "m2.db")
    store.set_setting(KEY, "{not json")
    leaked = RealAccountLimits(
        enabled=True, allowed_strategies=frozenset({STRATEGY_ID}),
        max_order_notional=2500.0, max_daily_notional=5000.0,
        require_manual_confirm=False, config_present=False,
        config_corrupt=False, corrupt_reason="",
    )
    monkeypatch.setattr(
        "futu_quant.real_trading.resolve_real_account_limits", lambda *a, **k: leaked,
    )

    decision = check_real_trade(
        store, _settings(monkeypatch), STRATEGY_ID, "US.SPY", "BUY", 1, 100.0, {},
    )

    # 变异后放行 —— 证明 check_real_trade 的 BUY 拦截确实挂在 config_corrupt 上
    assert decision.allowed is True


def test_mutation_removing_alert_would_be_detected(tmp_path, monkeypatch):
    """变异：去掉告警调用时，上面的告警用例会因 sent 为空而转红。"""
    store = TradingStore(tmp_path / "m3.db")
    store.set_setting(KEY, "{not json")
    monkeypatch.setattr(AlertManager, "send", lambda *a, **k: None)

    decision = check_real_trade(
        store, _settings(monkeypatch), STRATEGY_ID, "US.SPY", "BUY", 1, 100.0, {},
    )

    # 告警被抹掉后门禁判定本身必须照旧生效（不得因告警失败而放行）
    assert decision.allowed is False
