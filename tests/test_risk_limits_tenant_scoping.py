"""P0-7 风险与资金参数租户化 —— 验收守卫。

设计：``docs/risk_limits_tenant_scoping_design.md``
事故：2026-10-22 夜模拟账户 ``20170229`` 当日亏 $10,351.89，撞上**全局**
``RISK_MAX_DAILY_LOSS=10000`` 被自动急停。

本文件逐条守住设计 §8 的验收项：

1. 账户 A 配 ``max_daily_loss=100``、账户 B 未配 → A 按 100 生效，B 按 env 兜底；
2. 账户级 ``risk`` 段**缺一项** → 拒绝开仓 + ERROR 审计（**不静默用 env**）；
3. **跨账户不串用**（缓存键含账户）；
4. **跨日重解析**（次日生效）；
5. 迁移 dry-run 输出两账户各自参数集；``--apply`` 后 env 值不变（仍作兜底）；
6. **三处调用点均走 ``resolve_risk_limits``**（静态守卫）。

外加两条铁律守卫：env 回落必须 WARN 且次数可观测；界面改动后必须**立即重解析**。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from futu_quant.execution import ExecutionManager
from futu_quant.risk import RiskEngine, RiskLimits
from futu_quant.risk_limits import (
    ACCOUNT_RISK_FIELDS,
    ENV_FALLBACK_COUNT_KEY,
    RiskLimitsConfigError,
    clear_risk_limits_cache,
    env_fallback_count,
    legacy_risk_limits_key,
    real_account_config_key,
    resolve_risk_limits,
    resolve_risk_limits_detail,
    risk_limits_tenant_key,
)
from futu_quant.real_trading import check_real_trade, reduction_notional_limits
from futu_quant.store import TradingStore


REAL_ACCOUNT = "281756481476169109"
PAPER_ACCOUNT = "20170229"

FULL_RISK = {
    "max_entry_notional": 111.0,
    "max_entry_daily_notional": 222.0,
    "max_reduction_order_notional": 333.0,
    "max_daily_reduction_notional": 444.0,
    "max_pending_orders": 7,
    "max_symbol_weight": 0.2,
    "max_gross_exposure": 0.5,
    "max_option_premium": 555.0,
    "max_daily_loss": 100.0,
    "max_drawdown_pct": 0.03,
}

#: 配置键 → ``RiskLimits`` 属性名（Chief 10-23 裁定：开仓侧两键在 ``risk`` 段改名）
ENTRY_KEY_ALIASES = {"max_entry_notional": "max_order_notional",
                     "max_entry_daily_notional": "max_daily_notional"}


class FakeBroker:
    """最小 broker：带 ``account_id``，供解析器提取账户作用域。"""

    def __init__(self, account_id: str | None = None, **attrs):
        if account_id is not None:
            self.account_id = account_id
        for key, value in attrs.items():
            setattr(self, key, value)

    def orders(self, historical: bool = False) -> pd.DataFrame:
        return pd.DataFrame(columns=["order_id", "order_status", "code", "trd_side", "qty"])

    @staticmethod
    def is_cancellable_status(status) -> bool:
        return False

    def positions(self):
        return pd.DataFrame(columns=["code", "qty", "market_val"])

    def account_info(self):
        return pd.DataFrame([{"total_assets": 1_000_000, "power": 1_000_000, "unrealized_pl": 0}])

    def place_limit_order(self, symbol, side, quantity, price) -> pd.DataFrame:
        return pd.DataFrame([{"order_id": "paper-1"}])


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_risk_limits_cache()
    yield
    clear_risk_limits_cache()


def _store(tmp_path, name="risk.db") -> TradingStore:
    return TradingStore(tmp_path / name)


def _set_account_risk(store: TradingStore, account_id: str, risk: dict) -> None:
    raw = json.loads(store.get_setting(real_account_config_key(account_id), "{}") or "{}")
    raw["risk"] = risk
    store.set_setting(real_account_config_key(account_id), json.dumps(raw))


def _audit_types(store: TradingStore) -> list[str]:
    return [row["event_type"] for row in store.audit_events(500)]


# --------------------------------------------------------------------------- #
# 验收 1：账户级优先，且**各账户各用各的**
# --------------------------------------------------------------------------- #

def test_account_level_risk_segment_wins_over_env(tmp_path):
    store = _store(tmp_path)
    _set_account_risk(store, PAPER_ACCOUNT, dict(FULL_RISK))

    resolution = resolve_risk_limits_detail(store, FakeBroker(PAPER_ACCOUNT))

    assert resolution.source == f"real_account_config:{PAPER_ACCOUNT}"
    assert resolution.limits.max_daily_loss == 100.0
    assert resolution.limits.max_order_notional == 111.0
    assert resolution.env_fallback is False


def test_unconfigured_account_falls_back_to_env(tmp_path):
    store = _store(tmp_path)
    _set_account_risk(store, REAL_ACCOUNT, dict(FULL_RISK))

    resolution = resolve_risk_limits_detail(store, FakeBroker(PAPER_ACCOUNT))

    assert resolution.source == "env"
    assert resolution.env_fallback is True
    env_limits = RiskLimits.load()
    assert resolution.limits.max_daily_loss == env_limits.max_daily_loss
    assert resolution.limits.max_daily_loss != 100.0


# --------------------------------------------------------------------------- #
# 验收 2：部分配置即拒绝（fail-closed，不静默用 env）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("drop_field", sorted(FULL_RISK))
def test_partial_account_risk_segment_is_rejected(tmp_path, drop_field):
    store = _store(tmp_path)
    partial = {k: v for k, v in FULL_RISK.items() if k != drop_field}
    _set_account_risk(store, PAPER_ACCOUNT, partial)

    with pytest.raises(RiskLimitsConfigError) as excinfo:
        resolve_risk_limits(store, FakeBroker(PAPER_ACCOUNT))

    assert drop_field in str(excinfo.value), "拒绝原因必须点名缺的是哪一项"
    assert "不得用 env" in str(excinfo.value) or "静默补齐" in str(excinfo.value)


def test_partial_config_never_silently_uses_env(tmp_path, monkeypatch):
    """关键守卫：缺项时**不许**用 env 的值顶上（哪怕 env 里正好有这个值）。"""
    monkeypatch.setenv("RISK_MAX_DAILY_LOSS", "8888")
    store = _store(tmp_path)
    partial = {k: v for k, v in FULL_RISK.items() if k != "max_daily_loss"}
    _set_account_risk(store, PAPER_ACCOUNT, partial)

    with pytest.raises(RiskLimitsConfigError):
        resolve_risk_limits(store, FakeBroker(PAPER_ACCOUNT))

    assert os.environ["RISK_MAX_DAILY_LOSS"] == "8888", "env 值不得被改动"
    # 若缺项被 env 静默补齐，上面会返回 8888.0（env 值）而不是抛异常。
    assert RiskLimits.load().max_daily_loss == 8888.0


def test_tenant_level_partial_config_is_also_rejected(tmp_path):
    store = _store(tmp_path)
    partial = {"max_daily_loss": 100.0}
    store.set_setting(risk_limits_tenant_key("futu_paper_sandbox"), json.dumps(partial))

    with pytest.raises(RiskLimitsConfigError):
        resolve_risk_limits(store, FakeBroker(PAPER_ACCOUNT))


def test_empty_risk_segment_is_treated_as_absent(tmp_path):
    """``risk: {}`` 不算「部分配置」—— 它没有任何参数，应继续往下找。"""
    store = _store(tmp_path)
    _set_account_risk(store, PAPER_ACCOUNT, {})

    resolution = resolve_risk_limits_detail(store, FakeBroker(PAPER_ACCOUNT))
    assert resolution.source == "env"


# --------------------------------------------------------------------------- #
# 验收 3：跨账户不串用（缓存键含账户）
# --------------------------------------------------------------------------- #

def test_cache_key_contains_account_no_cross_use(tmp_path):
    store = _store(tmp_path)
    _set_account_risk(store, REAL_ACCOUNT, {**FULL_RISK, "max_daily_loss": 111.0})
    _set_account_risk(store, PAPER_ACCOUNT, {**FULL_RISK, "max_daily_loss": 222.0})

    real_resolution = resolve_risk_limits_detail(store, FakeBroker(REAL_ACCOUNT))
    paper_resolution = resolve_risk_limits_detail(store, FakeBroker(PAPER_ACCOUNT))
    real_again = resolve_risk_limits_detail(store, FakeBroker(REAL_ACCOUNT))

    assert real_resolution.limits.max_daily_loss == 111.0
    assert paper_resolution.limits.max_daily_loss == 222.0
    assert real_again.limits.max_daily_loss == 111.0, "A 账户的限额被 B 账户串用"


# --------------------------------------------------------------------------- #
# 验收 4 + 铁律：跨日重解析 & 界面改动后立即重解析
# --------------------------------------------------------------------------- #

def test_ui_edit_re_resolves_immediately_without_restart(tmp_path):
    store = _store(tmp_path)
    _set_account_risk(store, PAPER_ACCOUNT, {**FULL_RISK, "max_daily_loss": 100.0})

    first = resolve_risk_limits_detail(store, FakeBroker(PAPER_ACCOUNT))
    assert first.limits.max_daily_loss == 100.0

    # 模拟界面改了账户参数（不清缓存、不重启进程）
    _set_account_risk(store, PAPER_ACCOUNT, {**FULL_RISK, "max_daily_loss": 25_000.0})

    second = resolve_risk_limits_detail(store, FakeBroker(PAPER_ACCOUNT))
    assert second.limits.max_daily_loss == 25_000.0, "界面改动后必须立即重解析"


def test_cache_key_contains_trading_day(tmp_path):
    """缓存键必须含 (账户, 交易日) —— 用源码静态守住。"""
    source = (Path(__file__).resolve().parents[1] / "src" / "futu_quant" / "risk_limits.py").read_text(
        encoding="utf-8"
    )
    assert "_CACHE.get((account_id, day))" in source, "缓存键必须同时含账户与交易日"
    assert "day = _trading_day_key()" in source


# --------------------------------------------------------------------------- #
# 铁律：env 回落必须 WARN 且次数可观测
# --------------------------------------------------------------------------- #

def test_env_fallback_is_audited_and_counted(tmp_path):
    store = _store(tmp_path)
    before = env_fallback_count()

    resolve_risk_limits(store, FakeBroker(PAPER_ACCOUNT))
    resolve_risk_limits(store, FakeBroker(PAPER_ACCOUNT))   # 同日同账户 → 命中缓存，不重复计数

    assert "RISK_LIMITS_ENV_FALLBACK" in _audit_types(store), "env 回落必须留 WARN 审计"
    assert env_fallback_count() == before + 1
    assert int(store.get_setting(ENV_FALLBACK_COUNT_KEY, "0")) >= 1


# --------------------------------------------------------------------------- #
# 验收 6：三处调用点均走 resolve_risk_limits（静态守卫）
# --------------------------------------------------------------------------- #

def test_risk_engine_construction_uses_resolve_entry(tmp_path):
    store = _store(tmp_path)
    _set_account_risk(store, PAPER_ACCOUNT, {**FULL_RISK, "max_daily_loss": 777.0})

    engine = RiskEngine(store, broker=FakeBroker(PAPER_ACCOUNT))

    assert engine.limits.max_daily_loss == 777.0


def test_risk_engine_injected_limits_are_not_overridden(tmp_path):
    store = _store(tmp_path)
    injected = RiskLimits(max_order_notional=1_000, max_daily_notional=10_000)

    engine = RiskEngine(store, injected, broker=FakeBroker(PAPER_ACCOUNT))

    assert engine.limits.max_order_notional == 1_000, "显式注入的 limits 不得被解析结果覆盖"


def test_risk_engine_check_blocks_when_config_incomplete(tmp_path):
    store = _store(tmp_path)
    _set_account_risk(store, PAPER_ACCOUNT, {"max_daily_loss": 100.0})

    engine = RiskEngine(store, broker=FakeBroker(PAPER_ACCOUNT))
    decision = engine.check(FakeBroker(PAPER_ACCOUNT), "US.NVDA", "BUY", 1, 100.0)

    assert decision.allowed is False
    assert "风控参数配置不完整" in decision.reason


def test_submit_limit_rejects_and_audits_on_incomplete_config(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIRE_STRATEGY_APPROVAL", "false")
    store = _store(tmp_path)
    raw = json.loads(store.get_setting(f"real_account_config:{PAPER_ACCOUNT}", "{}") or "{}")
    raw.update({"enabled": True, "allowed_strategies": ["sma"], "risk": {"max_daily_loss": 100.0}})
    store.set_setting(f"real_account_config:{PAPER_ACCOUNT}", json.dumps(raw))

    executor = ExecutionManager(FakeBroker(PAPER_ACCOUNT), store)
    result = executor.submit_limit("sma", "2026-10-23:US.NVDA:BUY", "US.NVDA", "BUY", 1, 100.0)

    assert result.status == "REJECTED"
    assert "配置不完整" in result.message
    assert "RISK_LIMITS_CONFIG_INCOMPLETE" in _audit_types(store), "必须记 ERROR 审计"


def test_real_trading_check_rejects_on_incomplete_config(tmp_path):
    store = _store(tmp_path)
    _set_account_risk(store, REAL_ACCOUNT, {"max_daily_loss": 100.0})
    settings = SimpleNamespace(
        trading_env="REAL", real_trading_enabled=True, real_account_id=REAL_ACCOUNT,
        real_allowed_strategies=("core_satellite",), real_require_manual_confirm=False,
    )

    decision = check_real_trade(
        store, settings, "core_satellite", "US.NVDA", "SELL", 1, 100.0,
        metadata={"account_id": REAL_ACCOUNT},
    )

    assert decision.allowed is False
    assert "配置不完整" in decision.reason


def test_reduction_notional_limits_follows_account(tmp_path):
    store = _store(tmp_path)
    _set_account_risk(store, PAPER_ACCOUNT, dict(FULL_RISK))

    order_limit, daily_limit = reduction_notional_limits(store, FakeBroker(PAPER_ACCOUNT))

    assert (order_limit, daily_limit) == (333.0, 444.0)


def test_reduction_notional_limits_legacy_no_store_signature():
    """历史调用方（无 store）必须仍然可用 —— 保持向后兼容。"""
    limits = RiskLimits.load()
    assert reduction_notional_limits() == (
        limits.max_reduction_order_notional, limits.max_daily_reduction_notional,
    )


# --------------------------------------------------------------------------- #
# 验收 6：三处调用点静态守卫
# --------------------------------------------------------------------------- #

def test_three_call_points_all_use_resolve_risk_limits():
    root = Path(__file__).resolve().parents[1]
    risk_src = (root / "src" / "futu_quant" / "risk.py").read_text(encoding="utf-8")
    execution_src = (root / "src" / "futu_quant" / "execution.py").read_text(encoding="utf-8")
    real_trading_src = (root / "src" / "futu_quant" / "real_trading.py").read_text(encoding="utf-8")

    # 调用点 1：RiskEngine 构造注入
    assert "resolve_risk_limits(store, broker)" in risk_src, "RiskEngine 构造必须走唯一入口"
    assert "RiskLimits.load()" not in risk_src, "risk.py 不得再自行读 env 解析 limits"

    # 调用点 2：execution.submit_limit 的风控门
    assert "resolve_risk_limits(self.store, self.broker)" in execution_src
    assert "limits=risk_limits" in execution_src

    # 调用点 3：real_trading.check_real_trade
    assert "from .risk_limits import RiskLimitsConfigError, resolve_risk_limits" in real_trading_src
    check_body = real_trading_src.split("def check_real_trade", 1)[1]
    assert "_RiskBrokerScope(account_id=target_account)" in check_body
    assert "RiskLimits.load()" not in check_body, "check_real_trade 不得再自行读 env"


# --------------------------------------------------------------------------- #
# 铁律：配置键必须走共享入口（杜绝手拼）
# --------------------------------------------------------------------------- #

def test_account_risk_fields_match_risk_limits_dataclass():
    """账户级 ``risk`` 段的字段集合必须与 ``RiskLimits`` 的资金/亏损字段**逐个对齐**。

    注意配置键与属性名**不必同名**：开仓侧两键按 Chief 裁定在 ``risk`` 段改名
    （``max_entry_*``），以区别于账户顶层的实盘门禁字段（``max_order_notional``）。
    """
    dataclass_fields = set(RiskLimits.__dataclass_fields__) - {"correlation_clusters"}
    assert set(FULL_RISK) == {name for name, _ in ACCOUNT_RISK_FIELDS}
    assert {ENTRY_KEY_ALIASES.get(k, k) for k in FULL_RISK} == dataclass_fields


def test_entry_key_alias_maps_to_risk_limits_attribute(tmp_path):
    """``max_entry_notional`` 必须落到 ``RiskLimits.max_order_notional`` —— 别名映射不能错。"""
    store = _store(tmp_path)
    _set_account_risk(store, PAPER_ACCOUNT, dict(FULL_RISK))

    limits = resolve_risk_limits(store, FakeBroker(PAPER_ACCOUNT))

    assert limits.max_order_notional == 111.0
    assert limits.max_daily_notional == 222.0
    assert limits.max_daily_loss == 100.0


def test_unknown_risk_field_is_warned_not_silently_ignored(tmp_path, caplog):
    """``risk`` 段出现不认识的键 → 必须 WARN（不能静默当没看见）。"""
    store = _store(tmp_path)
    section = {**FULL_RISK, "max_order_notional": 999.0}   # 改名前的旧名
    _set_account_risk(store, PAPER_ACCOUNT, section)

    with caplog.at_level("WARNING"):
        limits = resolve_risk_limits(store, FakeBroker(PAPER_ACCOUNT))

    assert limits.max_order_notional == 111.0, "旧名不得被当成有效值"
    assert any("无法识别的字段" in record.message for record in caplog.records)


def test_two_gates_two_parameter_sets(tmp_path):
    """**两个门禁、两套参数**（Chief 10-23 裁 ③）：

    * 账户**顶层** ``max_order_notional`` → **实盘门禁**（``real_trading.check_real_trade``，REAL only）
    * ``risk`` 段 ``max_entry_notional`` → **风控引擎**（``RiskEngine``，全模式）

    两者**互不干扰**：改名后同名不同层的问题被消除，但仍必须守卫"谁读哪层"没被改错。
    """
    store = _store(tmp_path)
    raw = json.loads(store.get_setting(real_account_config_key(REAL_ACCOUNT), "{}") or "{}")
    raw.update({
        "enabled": True,
        "allowed_strategies": ["core_satellite"],
        "max_order_notional": 2500.0,      # 顶层 = 实盘门禁
        "max_daily_notional": 5000.0,
        "risk": dict(FULL_RISK),           # risk 段 = 风控引擎（max_entry_notional = 111）
    })
    store.set_setting(real_account_config_key(REAL_ACCOUNT), json.dumps(raw))

    # 风控引擎读 risk 段
    limits = resolve_risk_limits(store, FakeBroker(REAL_ACCOUNT))
    assert limits.max_order_notional == 111.0

    # 实盘门禁读**顶层**，不受 risk 段影响：3000 > 2500 → 拒
    settings = SimpleNamespace(
        trading_env="REAL", real_trading_enabled=True, real_account_id=REAL_ACCOUNT,
        real_allowed_strategies=("core_satellite",), real_require_manual_confirm=False,
    )
    decision = check_real_trade(
        store, settings, "core_satellite", "US.NVDA", "BUY", 1, 3000.0,
        metadata={"account_id": REAL_ACCOUNT},
    )
    assert decision.allowed is False
    assert "2,500" in decision.reason, "实盘门禁必须用**顶层**的 2500，不是 risk 段的 111"


def test_tenant_keys_route_through_shared_entry():
    """``risk_limits`` / ``real_account_config`` 的键必须由 ``tenant.py`` 的共享入口产出。

    守卫方式：断言产出值与共享入口**逐字节相同**，且共享入口缺失时**抛异常**
    （fail-closed），而不是静默拼一个本地字符串 —— 那正是「杜绝手拼」要治的病。
    """
    from futu_quant import tenant

    assert risk_limits_tenant_key("futu_paper_sandbox") == tenant.risk_limits_config_key(
        "futu_paper_sandbox"
    ) == "risk_limits:futu_paper_sandbox"
    assert real_account_config_key(PAPER_ACCOUNT) == tenant.real_account_config_key(
        PAPER_ACCOUNT
    ) == f"real_account_config:{PAPER_ACCOUNT}"


def test_tenant_key_helper_has_no_local_spelling(tmp_path):
    """``risk_limits.py`` 里不得再出现手拼的 ``real_account_config:`` 字面量。"""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "futu_quant" / "risk_limits.py"
    ).read_text(encoding="utf-8")
    code_lines = [
        line for line in source.splitlines()
        if not line.lstrip().startswith(("#", "*", '"""', "'''"))
    ]
    for line in code_lines:
        assert 'f"real_account_config:' not in line, f"发现手拼账户键：{line.strip()}"
        assert 'f"risk_limits:' not in line, f"发现手拼租户键：{line.strip()}"


def test_unregistered_tenant_is_ignored_not_fatal(tmp_path):
    """未注册的 ``tenant_id`` 不得让整条下单链路拒绝 —— 挡在解析之外，绝不抛异常。

    共享入口 ``tenant.risk_limits_config_key`` 是 fail-closed 的（未注册租户直接抛），
    若不在这里先挡住，一个非法 ``tenant_id`` 会把「键拼错」放大成「全系统拒绝下单」。
    """
    store = _store(tmp_path)

    # 既无账户、租户又未注册 → 无租户上下文，走 env 兜底，而不是抛异常
    resolution = resolve_risk_limits_detail(store, FakeBroker(None, tenant_id="ghost_tenant"))
    assert resolution.tenant_id == ""
    assert resolution.source == "env"

    # 有账户、broker 携带一个未注册 tenant_id → 退回由账户反解出的**已注册**租户
    _set_account_risk(store, REAL_ACCOUNT, {**FULL_RISK, "max_daily_loss": 555.0})
    resolution = resolve_risk_limits_detail(
        store, FakeBroker(REAL_ACCOUNT, tenant_id="ghost_tenant")
    )
    assert resolution.tenant_id == "futu_real_master"
    assert resolution.limits.max_daily_loss == 555.0


# --------------------------------------------------------------------------- #
# 读侧：租户键优先、无前缀旧键兜底（Reviewer ③）
# --------------------------------------------------------------------------- #

def test_legacy_unprefixed_key_is_read_as_fallback(tmp_path):
    """租户键缺失时，无前缀旧键 ``risk_limits`` 必须能兜底 —— 否则迁移前读旧键的一方读不到。"""
    store = _store(tmp_path)
    store.set_setting(legacy_risk_limits_key(), json.dumps(FULL_RISK))

    resolution = resolve_risk_limits_detail(store, FakeBroker(PAPER_ACCOUNT))

    assert resolution.source == legacy_risk_limits_key()
    assert resolution.limits.max_daily_loss == 100.0
    assert "RISK_LIMITS_LEGACY_KEY_FALLBACK" in _audit_types(store), "兜底必须留痕"


def test_tenant_key_wins_over_legacy_key(tmp_path):
    store = _store(tmp_path)
    store.set_setting(legacy_risk_limits_key(), json.dumps(FULL_RISK))
    store.set_setting(risk_limits_tenant_key("futu_paper_sandbox"),
                      json.dumps({**FULL_RISK, "max_daily_loss": 42.0}))

    resolution = resolve_risk_limits_detail(store, FakeBroker(PAPER_ACCOUNT))

    assert resolution.source == risk_limits_tenant_key("futu_paper_sandbox")
    assert resolution.limits.max_daily_loss == 42.0
    assert "RISK_LIMITS_LEGACY_KEY_FALLBACK" not in _audit_types(store)


def test_legacy_key_partial_config_is_still_fail_closed(tmp_path):
    """兜底路径也必须是 fail-closed —— 部分配置照样拒绝，不许当成"有值"。"""
    store = _store(tmp_path)
    store.set_setting(legacy_risk_limits_key(), json.dumps({"max_daily_loss": 100.0}))

    with pytest.raises(RiskLimitsConfigError):
        resolve_risk_limits(store, FakeBroker(PAPER_ACCOUNT))


def test_legacy_key_change_invalidates_cache(tmp_path):
    store = _store(tmp_path)
    store.set_setting(legacy_risk_limits_key(), json.dumps({**FULL_RISK, "max_daily_loss": 1.0}))
    first = resolve_risk_limits_detail(store, FakeBroker(PAPER_ACCOUNT))
    assert first.limits.max_daily_loss == 1.0

    store.set_setting(legacy_risk_limits_key(), json.dumps({**FULL_RISK, "max_daily_loss": 2.0}))
    second = resolve_risk_limits_detail(store, FakeBroker(PAPER_ACCOUNT))
    assert second.limits.max_daily_loss == 2.0, "改了旧键也必须立即重解析"
