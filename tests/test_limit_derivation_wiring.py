import json

import pytest

from futu_quant.limit_profile import LimitProfile, classify_changes, resolve_limit_chain, validate_profile
from futu_quant.limit_profile_store import load_limit_profile, profile_key, publish_limit_profile
from futu_quant.store import TradingStore


def test_profile_key_is_tenant_scoped():
    assert profile_key("futu_real_master") == "limit_profile:futu_real_master"
    with pytest.raises(ValueError):
        profile_key("futu_real_master:other")


def test_hard_cap_and_floor_are_rejected_before_publish():
    ok, reason = validate_profile(LimitProfile(per_order_ratio=6.0), 10000)
    assert not ok and "硬顶" in reason
    ok, reason = validate_profile(LimitProfile(per_order_ratio=0.0001), 10000)
    assert not ok and "地板" in reason
    ok, reason = validate_profile(LimitProfile(), 0)
    assert not ok and "权益" in reason


def test_change_classification_uses_effective_values():
    old = resolve_limit_chain(None, "futu_paper_sandbox", 10000, LimitProfile())
    new = resolve_limit_chain(None, "futu_paper_sandbox", 10000, LimitProfile(per_order_ratio=0.10))
    assert classify_changes(old, new)["per_order"] == "loosen"


def test_publish_round_trip_reads_runtime_config_and_audits(tmp_path):
    store = TradingStore(tmp_path / "limit-profile.db")
    tenant = "futu_paper_sandbox"
    result = publish_limit_profile(store, tenant, LimitProfile(), "streamlit_admin:test", "初始化派生限额", equity=10000)
    loaded = load_limit_profile(store, tenant)
    assert result["release_id"] > 0
    assert loaded.source == "derived:limit_profile"
    assert loaded.profile == LimitProfile()
    versions = store.config_versions(profile_key(tenant))
    assert versions and json.loads(versions[0]["value_json"])["per_order_ratio"] == 0.06
    audit = [x for x in store.audit_events() if x["event_type"] == "LIMIT_PROFILE_PUBLISHED"]
    assert audit
    payload = json.loads(audit[0]["payload_json"])
    assert payload["actor"] == "streamlit_admin:test" and payload["reason"] == "初始化派生限额"


def test_publish_failure_rolls_back_without_half_written_profile(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "limit-profile-rollback.db")
    tenant = "futu_paper_sandbox"
    first = LimitProfile(per_order_ratio=0.05)
    publish_limit_profile(store, tenant, first, "streamlit_admin:test", "旧值", equity=10000)
    original = store.publish_config_release_with_audit

    def fail(*args, **kwargs):
        raise RuntimeError("模拟事务中断")

    monkeypatch.setattr(store, "publish_config_release_with_audit", fail)
    with pytest.raises(RuntimeError, match="模拟事务中断"):
        publish_limit_profile(store, tenant, LimitProfile(per_order_ratio=0.06), "streamlit_admin:test", "新值", equity=10000)
    monkeypatch.setattr(store, "publish_config_release_with_audit", original)
    assert load_limit_profile(store, tenant).profile == first


def test_capacity_ratio_is_independent_of_per_order_ratio():
    low = LimitProfile(per_order_ratio=0.06, daily_entries_ratio=0.20, capacity_ratio=0.06)
    high = LimitProfile(per_order_ratio=0.20, daily_entries_ratio=0.20, capacity_ratio=0.06)
    low_chain = resolve_limit_chain(None, "futu_real_master", 6637.64, low)
    high_chain = resolve_limit_chain(None, "futu_real_master", 6637.64, high)
    assert low_chain.capacity_line == pytest.approx(high_chain.capacity_line)
    assert high_chain.capacity_line == pytest.approx(398.2584)
    assert high_chain.per_order > high_chain.capacity_line
    allowed, rejected_by, effective = __import__("futu_quant.limit_profile", fromlist=["effective_limit"]).effective_limit(high_chain, 398.3)
    assert not allowed and rejected_by in {"daily_entries", "capacity_line"} and effective <= high_chain.capacity_line
