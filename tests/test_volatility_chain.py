from datetime import datetime, timedelta, timezone

from futu_quant.volatility_chain import campaign_intent_chain_status


NOW = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)


def campaign(tenant="paper-a", age=901):
    return {
        "code": "US.AMD261106C200000",
        "tenant_id": tenant,
        "activation_trace_id": "trace-amd",
        "auto_created_at": (NOW - timedelta(seconds=age)).isoformat(),
    }


def test_campaign_without_intent_or_gate_is_broken_after_timeout():
    broken, reason = campaign_intent_chain_status(
        campaign(), now=NOW, intents=[], decisions=[], tenant_id="paper-a",
    )
    assert broken is True
    assert "trace-amd" in reason


def test_campaign_waiting_before_timeout_is_not_broken():
    broken, reason = campaign_intent_chain_status(
        campaign(age=899), now=NOW, intents=[], decisions=[], tenant_id="paper-a",
    )
    assert broken is False
    assert "等待中" in reason


def test_normal_gate_decision_prevents_false_chain_alert():
    c = campaign()
    decisions = [{
        "tenant_id": "paper-a", "strategy_id": "volatility_long_gamma",
        "symbol": c["code"], "decision": "BLOCKED", "created_at": NOW.isoformat(),
    }]
    broken, reason = campaign_intent_chain_status(
        c, now=NOW, intents=[], decisions=decisions, tenant_id="paper-a",
    )
    assert broken is False
    assert "门禁" in reason


def test_intent_is_tenant_isolated():
    c = campaign()
    other_tenant_intent = [{
        "tenant_id": "paper-b", "strategy_id": "volatility_long_gamma",
        "symbol": c["code"], "created_at": NOW.isoformat(),
    }]
    broken, _ = campaign_intent_chain_status(
        c, now=NOW, intents=other_tenant_intent, decisions=[], tenant_id="paper-a",
    )
    assert broken is True
    own_intent = [{**other_tenant_intent[0], "tenant_id": "paper-a"}]
    broken, reason = campaign_intent_chain_status(
        c, now=NOW, intents=own_intent, decisions=[], tenant_id="paper-a",
    )
    assert broken is False
    assert "已形成" in reason


def test_legacy_or_invalid_campaign_is_fail_safe_without_false_alert():
    broken, reason = campaign_intent_chain_status(
        {"code": "US.AMD261106C200000"}, now=NOW,
        intents=[], decisions=[], tenant_id="paper-a",
    )
    assert broken is False
    assert "存量" in reason

    invalid = campaign()
    invalid["auto_created_at"] = "not-a-date"
    broken, reason = campaign_intent_chain_status(
        invalid, now=NOW, intents=[], decisions=[], tenant_id="paper-a",
    )
    assert broken is False
    assert "时间无效" in reason
