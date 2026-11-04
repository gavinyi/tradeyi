"""派生层边界守卫：旧键回潮、批预算层接线、草稿清理与回落审计。

Reviewer 2026-11-04 阻断 2：`real_trading` / `risk_limits` 仍是「先读旧键金额、
再覆盖」，没有任何用例断言「派生启用后改旧键金额，生效值不变」—— 回潮通道不会变红。
本文件把这条通道钉死，并覆盖第 5 层批预算与两条次要项。
"""
from types import SimpleNamespace

import pytest

import futu_quant.real_trading as real_trading
import futu_quant.tenant as tenant_module
from futu_quant.limit_profile import LimitProfile, resolve_batch_budget
from futu_quant.limit_profile_store import publish_limit_profile
from futu_quant.risk_limits import resolve_risk_limits, risk_limits_tenant_key
from futu_quant.store import TradingStore

REAL_ACCOUNT = "281756481476169109"
REAL_TENANT = "futu_real_master"
PAPER_TENANT = "futu_paper_sandbox"
REAL_EQUITY = 6_637.64
PAPER_EQUITY = 982_356.163


def _settings(**overrides):
    base = dict(
        real_max_order_notional=1_000.0,
        real_max_daily_notional=2_500.0,
        real_allowed_strategies=("core_satellite",),
        real_require_manual_confirm=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _complete_risk_section(**overrides):
    base = {
        "max_entry_notional": 2_500.0,
        "max_entry_daily_notional": 5_000.0,
        "max_reduction_order_notional": 150_000.0,
        "max_daily_reduction_notional": 500_000.0,
        "max_pending_orders": 20,
        "max_symbol_weight": 0.25,
        "max_gross_exposure": 0.85,
        "max_option_premium": 30_000.0,
        "max_daily_loss": 100_000.0,
        "max_drawdown_pct": 0.05,
    }
    base.update(overrides)
    return base


def _legacy_limits(per_order=1_000.0, daily=2_500.0, loss=400.0):
    return SimpleNamespace(
        max_order_notional=per_order, max_daily_notional=daily, max_daily_loss=loss
    )


def _publish_real_profile(store):
    return publish_limit_profile(
        store, REAL_TENANT,
        LimitProfile(per_order_ratio=0.06, daily_entries_ratio=0.06,
                     daily_loss_ratio=0.06, capacity_ratio=0.06),
        "streamlit_admin:test", "实盘派生限额",
        legacy_limits=_legacy_limits(), equity=REAL_EQUITY,
        approval_fingerprint="fp-real", confirm=True,
    )


# ---------------------------------------------------------------------------
# 1. 旧键回潮：派生启用后改旧键金额，生效值必须不变
# ---------------------------------------------------------------------------


def test_real_limits_ignore_legacy_top_level_amounts_once_derived(monkeypatch, tmp_path):
    """`real_account_config` 顶层金额是旧键：派生启用后改它不得影响生效值。"""
    import json

    store = TradingStore(tmp_path / "real-no-backflow.db")
    _publish_real_profile(store)
    store.record_equity(REAL_EQUITY, 0.0, REAL_EQUITY, env=f"REAL_{REAL_ACCOUNT}")
    store.set_setting(
        f"real_account_config:{REAL_ACCOUNT}",
        json.dumps({"enabled": True, "max_order_notional": 1_000.0,
                    "max_daily_notional": 2_500.0}),
    )
    monkeypatch.setattr(
        tenant_module, "resolve_tenant",
        lambda **_: SimpleNamespace(tenant_id=REAL_TENANT),
    )

    before = real_trading.resolve_real_account_limits(store, _settings(), REAL_ACCOUNT)
    assert before.limit_profile_unresolved is False
    effective_before = before.max_order_notional
    assert effective_before == pytest.approx(398.26, abs=0.02)

    # 把旧键金额放大到 9,999 —— 若还用旧键覆盖，生效值会跟着变
    store.set_setting(
        f"real_account_config:{REAL_ACCOUNT}",
        json.dumps({"enabled": True, "max_order_notional": 9_999.0,
                    "max_daily_notional": 9_999.0}),
    )
    after = real_trading.resolve_real_account_limits(store, _settings(), REAL_ACCOUNT)
    assert after.max_order_notional == pytest.approx(effective_before, abs=0.001)
    assert after.max_order_notional != pytest.approx(9_999.0)
    assert after.max_daily_notional != pytest.approx(9_999.0)


def test_risk_limits_ignore_legacy_risk_section_once_derived(tmp_path):
    """租户 `risk_limits` 旧金额键是回潮通道：派生启用后改它不得影响生效值。"""
    import json

    store = TradingStore(tmp_path / "risk-no-backflow.db")
    store.record_equity(PAPER_EQUITY, 0.0, PAPER_EQUITY, env="SIMULATE")
    publish_limit_profile(
        store, PAPER_TENANT, LimitProfile(), "streamlit_admin:test", "模拟盘派生限额",
        legacy_limits=_legacy_limits(2_500.0, 5_000.0, 100_000.0),
        equity=PAPER_EQUITY, approval_fingerprint="fp-paper", confirm=True,
    )
    store.set_setting(
        risk_limits_tenant_key(PAPER_TENANT), json.dumps(_complete_risk_section())
    )
    scope = SimpleNamespace(account_id="", tenant_id=PAPER_TENANT)

    before = resolve_risk_limits(store, scope)
    assert before.max_order_notional == pytest.approx(50_000.0)  # 硬顶截断
    effective_before = before.max_order_notional

    store.set_setting(
        risk_limits_tenant_key(PAPER_TENANT),
        json.dumps(_complete_risk_section(max_entry_notional=77_777.0)),
    )
    after = resolve_risk_limits(store, scope)
    assert after.max_order_notional == pytest.approx(effective_before, abs=0.001)
    assert after.max_order_notional != pytest.approx(77_777.0)


# ---------------------------------------------------------------------------
# 2. 第 5 层：批预算必须接派生
# ---------------------------------------------------------------------------


def test_batch_budget_uses_derived_chain_not_campaign_amount(tmp_path):
    """资产 98 万模拟盘：批预算取派生 `batch`（硬顶 50,000），旧键 29,478 失效。"""
    store = TradingStore(tmp_path / "batch.db")
    publish_limit_profile(
        store, PAPER_TENANT, LimitProfile(), "streamlit_admin:test", "模拟盘派生限额",
        legacy_limits=_legacy_limits(2_500.0, 5_000.0, 100_000.0),
        equity=PAPER_EQUITY, approval_fingerprint="fp-paper", confirm=True,
    )
    budget, source = resolve_batch_budget(store, PAPER_TENANT, 29_478.0, PAPER_EQUITY)

    assert budget == pytest.approx(50_000.0)
    assert source == "derived:limit_profile"
    assert budget != pytest.approx(29_478.0)


def test_batch_budget_falls_back_to_legacy_only_before_publish(tmp_path):
    store = TradingStore(tmp_path / "batch-legacy.db")
    budget, source = resolve_batch_budget(store, PAPER_TENANT, 29_478.0, PAPER_EQUITY)

    assert budget == pytest.approx(29_478.0)
    assert source == "legacy:read_only"


def test_batch_budget_fails_closed_without_equity(tmp_path):
    """派生启用但拿不到权益 → 批预算 0（一张不买），绝不回落旧键金额。"""
    store = TradingStore(tmp_path / "batch-no-equity.db")
    publish_limit_profile(
        store, PAPER_TENANT, LimitProfile(), "streamlit_admin:test", "模拟盘派生限额",
        legacy_limits=_legacy_limits(2_500.0, 5_000.0, 100_000.0),
        equity=PAPER_EQUITY, approval_fingerprint="fp-paper", confirm=True,
    )
    budget, source = resolve_batch_budget(store, PAPER_TENANT, 29_478.0, None)

    assert budget == 0.0
    assert source == "derived:missing_equity"


# ---------------------------------------------------------------------------
# 3. 次要项：孤儿草稿与回落审计
# ---------------------------------------------------------------------------


def test_publish_failure_leaves_no_orphan_draft(tmp_path, monkeypatch):
    """发布事务失败后草稿必须作废，不能留孤儿 DRAFT。"""
    store = TradingStore(tmp_path / "orphan.db")
    publish_limit_profile(
        store, PAPER_TENANT, LimitProfile(per_order_ratio=0.05), "streamlit_admin:test", "旧值",
        legacy_limits=_legacy_limits(100.0, 200.0, 200.0), equity=10_000,
        confirm=True, approval_fingerprint="fp-1",
    )

    def fail(*args, **kwargs):
        raise RuntimeError("模拟事务中断")

    monkeypatch.setattr(store, "publish_config_release_with_audit", fail)
    with pytest.raises(RuntimeError, match="模拟事务中断"):
        publish_limit_profile(
            store, PAPER_TENANT, LimitProfile(per_order_ratio=0.06), "streamlit_admin:test", "新值",
            equity=10_000, confirm=True, approval_fingerprint="fp-2",
        )
    monkeypatch.undo()
    releases = store.config_releases()
    assert releases
    assert all(str(row["status"]) != "DRAFT" for row in releases), (
        "发布失败留下了孤儿 DRAFT 草稿"
    )


def test_legacy_fallback_writes_queryable_audit_event(monkeypatch, tmp_path):
    """租户解析失败走旧键时，必须留可查询审计，而不是只有一行日志。"""
    store = TradingStore(tmp_path / "legacy-audit.db")
    store.set_setting(f"real_account_config:{REAL_ACCOUNT}", '{"enabled": true}')

    def boom(**_kwargs):
        raise RuntimeError("租户表不可读")

    monkeypatch.setattr(tenant_module, "resolve_tenant", boom)
    limits = real_trading.resolve_real_account_limits(store, _settings(), REAL_ACCOUNT)

    assert limits.limit_profile_unresolved is False  # 未启用派生层不得误判 fail-closed
    assert limits.max_order_notional == pytest.approx(1_000.0)  # 走旧键
    events = [x["event_type"] for x in store.audit_events()]
    assert "LIMIT_PROFILE_LEGACY_FALLBACK" in events


# ---------------------------------------------------------------------------
# 4. store 不支持 runtime_config 读（第三轮全量真回归）与批预算零值告警
# ---------------------------------------------------------------------------


def test_store_without_runtime_reader_falls_back_to_legacy_and_records():
    """`store` 替身没有 `get_runtime_config` 时必须按未启用派生走旧键，且留痕。"""
    from futu_quant.limit_profile_store import load_limit_profile

    events: list[dict] = []

    class LimitedStore:
        def operational_event(self, component, event_type, status, payload=None):
            events.append({"event_type": event_type, "status": status, "payload": payload or {}})

    resolution = load_limit_profile(LimitedStore(), PAPER_TENANT)

    assert resolution.source == "legacy:read_only"
    assert resolution.profile == LimitProfile()
    assert events and events[0]["event_type"] == "LIMIT_PROFILE_STORE_UNSUPPORTED"
    assert events[0]["status"] == "WARNING"


def test_batch_budget_zero_emits_error_event(monkeypatch):
    """派生启用但无权益 → 批预算 0 必须发 ERROR 事件（不能只靠日志）。"""
    import futu_quant.volatility_roll as volatility_roll

    events: list[dict] = []

    class Store:
        def operational_event(self, component, event_type, status, payload=None):
            events.append({
                "component": component, "event_type": event_type,
                "status": status, "payload": payload or {},
            })

    volatility_roll.report_batch_budget_unresolved(Store(), PAPER_TENANT, "US.META", 29_478.0)

    assert events, "无权益导致批预算 0 时必须落 ERROR 事件"
    assert events[0]["event_type"] == "VOLATILITY_BATCH_BUDGET_UNRESOLVED"
    assert events[0]["status"] == "ERROR"
    assert events[0]["payload"]["legacy_budget"] == pytest.approx(29_478.0)

