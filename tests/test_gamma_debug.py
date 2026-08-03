from __future__ import annotations

from futu_quant.gamma_debug import diagnose_campaign, hard_campaign_issues


def test_gamma_debug_detects_calendar_mismatch_and_early_expiry():
    campaign = {
        "code": "US.GOOG680717C365000", "underlying": "US.GOOG",
        "event_date": "2068-07-15", "event_session": "盘前", "event_confirmed": True,
    }
    events = [{"symbol": "US.GOOG", "event_date": "2068-07-22", "session": "盘后",
               "confirmed": 1}]

    issues = diagnose_campaign(campaign, events)

    assert any("不一致" in issue for issue in issues)
    assert any("早于财报日" in issue for issue in issues)
    assert hard_campaign_issues(campaign, issues) == issues

    volatility_campaign = {**campaign, "trigger_mode": "VOLATILITY", "event_confirmed": False}
    volatility_issues = diagnose_campaign(volatility_campaign, events)
    assert volatility_issues == []
    assert hard_campaign_issues(volatility_campaign, volatility_issues) == []


def test_earnings_mode_requires_double_source_confirmation():
    campaign = {
        "code": "US.NVDA260717C180000", "underlying": "US.NVDA",
        "event_date": "2026-07-15", "event_session": "盘后", "event_confirmed": True,
        "trigger_mode": "EARNINGS",
    }

    issues = diagnose_campaign(campaign, [])

    assert "未找到富途与 Nasdaq 双源一致的财报时间" in issues
    assert hard_campaign_issues(campaign, issues) == issues


def test_earnings_manual_override_turns_double_source_gap_into_warning():
    campaign = {
        "code": "US.NVDA680717C180000", "underlying": "US.NVDA",
        "event_date": "2068-07-15", "event_session": "盘后",
        "event_confirmed": True, "trigger_mode": "EARNINGS",
        "event_validation_policy": "MANUAL_OVERRIDE",
    }

    issues = diagnose_campaign(campaign, [])

    assert any("人工确认调试模式" in issue for issue in issues)
    assert hard_campaign_issues(campaign, issues) == []
