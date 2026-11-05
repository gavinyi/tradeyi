import json
from types import SimpleNamespace

import pytest

from futu_quant.limit_profile import LimitProfile, classify_changes, resolve_limit_chain, validate_profile
from futu_quant.limit_profile_store import (
    load_limit_profile,
    profile_key,
    publish_limit_profile,
    resolve_baseline_chain,
)
from futu_quant.store import TradingStore

TENANT = "futu_paper_sandbox"


def _legacy(per_order=2_500.0, daily=5_000.0, loss=100_000.0):
    return SimpleNamespace(
        max_order_notional=per_order, max_daily_notional=daily, max_daily_loss=loss
    )


def test_profile_key_is_tenant_scoped():
    assert profile_key("futu_real_master") == "limit_profile:futu_real_master"
    with pytest.raises(ValueError):
        profile_key("futu_real_master:other")


def test_invalid_ratios_are_rejected_before_publish():
    """非法比例必须拒绝；超硬顶不算非法（硬顶负责截断，模拟盘 6% 就是这种情形）。"""
    ok, reason = validate_profile(LimitProfile(per_order_ratio=-0.1), 10000)
    assert not ok
    ok, reason = validate_profile(LimitProfile(per_order_ratio=float("nan")), 10000)
    assert not ok
    ok, reason = validate_profile(LimitProfile(per_order_ratio=0.0001), 10000)
    assert not ok and "地板" in reason
    ok, reason = validate_profile(LimitProfile(), 0)
    assert not ok and "权益" in reason
    ok, reason = validate_profile(LimitProfile(capacity_ratio=1.5), 10000)
    assert not ok and "capacity_ratio" in reason
    # 超硬顶允许，由 resolve 截断为硬顶值
    ok, reason = validate_profile(LimitProfile(per_order_ratio=6.0), 10000)
    assert ok and reason == ""


def test_change_classification_uses_effective_values():
    old = resolve_limit_chain(None, "futu_paper_sandbox", 10000, LimitProfile())
    # 默认 per_order_ratio 已是 0.20；收紧到 0.10 必须 classify 为 tighten。
    new = resolve_limit_chain(None, "futu_paper_sandbox", 10000, LimitProfile(per_order_ratio=0.10))
    assert classify_changes(old, new)["per_order"] == "tighten"
    wider = resolve_limit_chain(None, "futu_paper_sandbox", 10000, LimitProfile(per_order_ratio=0.30))
    assert classify_changes(old, wider)["per_order"] == "loosen"


def test_first_enable_is_classified_as_loosen_against_old_key_amount(tmp_path):
    """首启用 legacy→derived 必须按变松判定：旧键 $2,500 → 派生 $50,000（20 倍）。"""
    store = TradingStore(tmp_path / "baseline.db")
    equity = 982_356.163
    baseline = resolve_baseline_chain(store, TENANT, equity, _legacy())
    new_chain = resolve_limit_chain(store, TENANT, equity, LimitProfile())

    assert baseline.per_order == 2_500.0  # 基线来自旧键实际生效值，不是预览链
    assert new_chain.per_order == 50_000.0
    assert classify_changes(baseline, new_chain)["per_order"] == "loosen"


def test_first_enable_without_confirm_is_rejected(tmp_path):
    """首启用若判为变松，缺二次确认或审批指纹必须拒绝，不得静默放宽。"""
    store = TradingStore(tmp_path / "no-confirm.db")
    equity = 982_356.163

    with pytest.raises(PermissionError, match="变松"):
        publish_limit_profile(
            store, TENANT, LimitProfile(), "streamlit_admin:test", "首启用派生限额",
            legacy_limits=_legacy(), equity=equity,
        )
    with pytest.raises(PermissionError, match="变松"):
        publish_limit_profile(
            store, TENANT, LimitProfile(), "streamlit_admin:test", "首启用派生限额",
            legacy_limits=_legacy(), equity=equity, confirm=True,  # 缺审批指纹
        )
    assert load_limit_profile(store, TENANT).source == "legacy:read_only"


def test_first_enable_without_legacy_baseline_is_rejected(tmp_path):
    """拿不到旧键生效值时无法判定变松，必须 fail-closed 拒绝发布。"""
    store = TradingStore(tmp_path / "no-baseline.db")
    with pytest.raises(ValueError, match="基线"):
        publish_limit_profile(
            store, TENANT, LimitProfile(), "streamlit_admin:test", "无基线",
            equity=10_000, confirm=True, approval_fingerprint="fp",
        )


def test_publish_round_trip_reads_runtime_config_and_audits(tmp_path):
    store = TradingStore(tmp_path / "limit-profile.db")
    result = publish_limit_profile(
        store, TENANT, LimitProfile(), "streamlit_admin:test", "初始化派生限额",
        legacy_limits=_legacy(), equity=10000, confirm=True, approval_fingerprint="fp-1",
    )
    loaded = load_limit_profile(store, TENANT)
    assert result["release_id"] > 0
    assert loaded.source == "derived:limit_profile"
    assert loaded.profile == LimitProfile()
    versions = store.config_versions(profile_key(TENANT))
    assert versions and json.loads(versions[0]["value_json"])["per_order_ratio"] == 0.20
    audit = [x for x in store.audit_events() if x["event_type"] == "LIMIT_PROFILE_PUBLISHED"]
    assert audit
    payload = json.loads(audit[0]["payload_json"])
    assert payload["actor"] == "streamlit_admin:test" and payload["reason"] == "初始化派生限额"
    # 审计必须能复现「当时实际放行多少」，不能被 runtime_config 原文覆盖掉
    assert payload["effective_before"]["per_order"] == 2_500.0
    assert payload["effective_after"]["per_order"] == 2_000.0  # 权益 10,000 × 20%
    assert payload["changes"]["per_order"] == "tighten"


def test_publish_failure_rolls_back_without_half_written_profile(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "limit-profile-rollback.db")
    first = LimitProfile(per_order_ratio=0.05)
    publish_limit_profile(
        store, TENANT, first, "streamlit_admin:test", "旧值",
        legacy_limits=_legacy(per_order=100.0, daily=200.0, loss=200.0),
        equity=10000, confirm=True, approval_fingerprint="fp-1",
    )
    original = store.publish_config_release_with_audit

    def fail(*args, **kwargs):
        raise RuntimeError("模拟事务中断")

    monkeypatch.setattr(store, "publish_config_release_with_audit", fail)
    with pytest.raises(RuntimeError, match="模拟事务中断"):
        publish_limit_profile(
            store, TENANT, LimitProfile(per_order_ratio=0.06), "streamlit_admin:test", "新值",
            equity=10000, confirm=True, approval_fingerprint="fp-2",
        )
    monkeypatch.setattr(store, "publish_config_release_with_audit", original)
    assert load_limit_profile(store, TENANT).profile == first


def test_capacity_ratio_is_independent_of_per_order_ratio():
    # 容量线只受 capacity_ratio 与日亏预算钳制，与 per_order_ratio 无关。
    # ratio=0.20 < 默认 20%？相等 → capacity_line = 权益×20%，再被日亏（0.20）钳到同值。
    low = LimitProfile(per_order_ratio=0.06, daily_loss_ratio=0.20, capacity_ratio=0.20)
    high = LimitProfile(per_order_ratio=0.20, daily_loss_ratio=0.20, capacity_ratio=0.20)
    low_chain = resolve_limit_chain(None, "futu_real_master", 6637.64, low)
    high_chain = resolve_limit_chain(None, "futu_real_master", 6637.64, high)
    assert low_chain.capacity_line == pytest.approx(high_chain.capacity_line)
    assert high_chain.capacity_line == pytest.approx(1327.528)
    assert high_chain.capacity_line == pytest.approx(high_chain.daily_loss)
    # per_order 与容量线此时同值（0.20=0.20）；换 per_order_ratio 也不影响 capacity_line。
    tighter_per_order = resolve_limit_chain(
        None, "futu_real_master", 6637.64,
        LimitProfile(per_order_ratio=0.10, daily_loss_ratio=0.20, capacity_ratio=0.20),
    )
    assert tighter_per_order.per_order < tighter_per_order.capacity_line
    assert tighter_per_order.capacity_line == pytest.approx(high_chain.capacity_line)


def test_capacity_ratio_is_clamped_and_classified():
    """容量线也必须过 clamp 并进变松分类，否则调大容量线可绕过确认。"""
    # 日亏预算放宽到 30%，避免 daily_loss 钳制盖住 capacity_ratio 语义。
    wide = LimitProfile(daily_loss_ratio=0.30, capacity_ratio=5.0)
    chain = resolve_limit_chain(None, "futu_real_master", 10_000, wide)
    # ratio 5.0 → 钳到 1.0 → 10,000；与默认 20%（2,000）取 min → 2,000。
    assert chain.capacity_line == pytest.approx(2_000.0)
    assert chain.clamped["capacity_line"].startswith("hard_cap")

    base = resolve_limit_chain(
        None, "futu_real_master", 10_000,
        LimitProfile(daily_loss_ratio=0.30, capacity_ratio=0.06),
    )
    grown = resolve_limit_chain(
        None, "futu_real_master", 10_000,
        LimitProfile(daily_loss_ratio=0.30, capacity_ratio=0.10),
    )
    assert base.capacity_line == pytest.approx(600.0)
    assert grown.capacity_line == pytest.approx(1_000.0)
    assert classify_changes(base, grown)["capacity_line"] == "loosen"
    assert classify_changes(grown, base)["capacity_line"] == "tighten"
