"""派生限额的缓存必须以「权益快照」为新鲜度依据。

Chief 2026-11-04 待办：`resolve_risk_limits_detail` 的缓存键是「账户 + 交易日」，
新鲜度只看配置指纹、**不含权益**。派生限额 = 权益 × 百分比，于是当天第一次解析后
权益再跌，风控层会拿着更高权益算出的限额放行 —— 偏松，正是最危险的方向。

本文件锁三件事：
1. 权益下降后必须立刻重算（不允许复用旧值）；
2. 权益不变时缓存仍然命中（不能为了安全把缓存废掉）；
3. 变异：把权益从指纹里去掉 → 第 1 条必须变红。
"""
from types import SimpleNamespace

import pytest

import futu_quant.risk_limits as risk_limits
from futu_quant.limit_profile import LimitProfile
from futu_quant.limit_profile_store import publish_limit_profile
from futu_quant.risk_limits import clear_risk_limits_cache, resolve_risk_limits, risk_limits_tenant_key
from futu_quant.store import TradingStore

TENANT = "futu_paper_sandbox"
START_EQUITY = 6_671.0  # 6% → 400.26
DROPPED_EQUITY = 3_000.0  # 6% → 180.00


def _complete_risk_section():
    return {
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


def _ready_store(tmp_path, equity=START_EQUITY):
    import json

    store = TradingStore(tmp_path / "cache-equity.db")
    store.record_equity(equity, 0.0, equity, env="SIMULATE")
    publish_limit_profile(
        store, TENANT, LimitProfile(), "streamlit_admin:test", "模拟盘派生限额",
        legacy_limits=SimpleNamespace(
            max_order_notional=2_500.0, max_daily_notional=5_000.0, max_daily_loss=100_000.0
        ),
        equity=equity, approval_fingerprint="fp-cache", confirm=True,
    )
    store.set_setting(risk_limits_tenant_key(TENANT), json.dumps(_complete_risk_section()))
    clear_risk_limits_cache()
    return store


def _scope():
    return SimpleNamespace(account_id="", tenant_id=TENANT)


def test_limit_drops_immediately_after_equity_drops(tmp_path):
    """权益 6,671 → 3,000 后，限额必须从 400.26 降到 180，不得复用旧值。"""
    store = _ready_store(tmp_path)
    scope = _scope()

    before = resolve_risk_limits(store, scope)
    assert before.max_order_notional == pytest.approx(400.26, abs=0.02)

    store.record_equity(DROPPED_EQUITY, 0.0, DROPPED_EQUITY, env="SIMULATE")
    after = resolve_risk_limits(store, scope)

    assert after.max_order_notional == pytest.approx(180.0, abs=0.02)
    assert after.max_order_notional != pytest.approx(400.26, abs=0.02)


def test_cache_still_hits_when_equity_unchanged(tmp_path, monkeypatch):
    """权益不变时不能反复重算：同一快照下第二次解析必须命中缓存。"""
    store = _ready_store(tmp_path)
    scope = _scope()
    calls = {"n": 0}
    original = risk_limits._resolve_uncached

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(risk_limits, "_resolve_uncached", counted)

    resolve_risk_limits(store, scope)
    resolve_risk_limits(store, scope)
    assert calls["n"] == 1, "权益未变时缓存必须命中，不能每轮重算"

    store.record_equity(DROPPED_EQUITY, 0.0, DROPPED_EQUITY, env="SIMULATE")
    resolve_risk_limits(store, scope)
    assert calls["n"] == 2, "权益变化必须触发一次重算"


def test_missing_equity_snapshot_never_reuses_old_cache(tmp_path):
    """派生启用但快照缺失：指纹必须与旧缓存不等，不得复用旧值。"""
    store = _ready_store(tmp_path)
    scope = _scope()
    first = resolve_risk_limits(store, scope)
    assert first.max_order_notional == pytest.approx(400.26, abs=0.02)

    # 快照被清空（例如权益表异常）：不得拿旧指纹命中旧值
    store.equity_history = lambda **_: []
    with pytest.raises(risk_limits.RiskLimitsConfigError):
        resolve_risk_limits(store, scope)


def test_mutation_equity_not_in_fingerprint_fails_guard(tmp_path, monkeypatch):
    """变异：指纹从头就不含权益 → 权益下降后仍返回 400.26，守卫必须变红。"""
    store = _ready_store(tmp_path)
    scope = _scope()
    # 变异必须在**第一次解析之前**生效：否则补丁本身改变了指纹，
    # 缓存会被动失效，反而掩盖“跨轮复用旧权益”这个真问题。
    monkeypatch.setattr(risk_limits, "_equity_fingerprint_part", lambda *a, **k: None)

    first = resolve_risk_limits(store, scope)
    assert first.max_order_notional == pytest.approx(400.26, abs=0.02)

    store.record_equity(DROPPED_EQUITY, 0.0, DROPPED_EQUITY, env="SIMULATE")

    with pytest.raises(AssertionError):
        stale = resolve_risk_limits(store, scope)
        assert stale.max_order_notional == pytest.approx(180.0, abs=0.02), (
            "权益已降 55%，限额却仍是旧值 —— 指纹没带权益"
        )
