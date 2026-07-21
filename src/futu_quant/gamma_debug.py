from __future__ import annotations

from datetime import date, datetime
import re


EXPIRY = re.compile(r"^US\.[A-Z.]+(\d{6})[CP]\d+$")


def diagnose_campaign(campaign: dict, events: list[dict]) -> list[str]:
    issues = []
    trigger_mode = str(campaign.get("trigger_mode", "EARNINGS")).upper()
    underlying = str(campaign.get("underlying", ""))
    event_date = str(campaign.get("event_date", ""))
    confirmed = [event for event in events if event["symbol"] == underlying and event["confirmed"]]
    effective_event_date = event_date
    if trigger_mode == "EARNINGS":
        if not campaign.get("event_confirmed"):
            issues.append("财报时间未人工确认")
        if not confirmed:
            issues.append("未找到富途与 Nasdaq 双源一致的财报时间")
        else:
            latest = confirmed[0]
            effective_event_date = latest["event_date"]
            if (event_date, campaign.get("event_session")) != (latest["event_date"], latest["session"]):
                issues.append(
                    f"配置财报时间 {event_date} {campaign.get('event_session')} 与双源日历 "
                    f"{latest['event_date']} {latest['session']} 不一致"
                )
    match = EXPIRY.match(str(campaign.get("code", "")))
    if not match:
        issues.append("期权代码无法识别到期日")
    else:
        expiry = datetime.strptime(match.group(1), "%y%m%d").date()
        if expiry < date.today():
            issues.append(f"合约已于 {expiry} 到期")
        if trigger_mode == "EARNINGS":
            try:
                event_day = date.fromisoformat(effective_event_date)
                if expiry < event_day:
                    issues.append(f"合约到期日 {expiry} 早于财报日 {event_day}")
            except ValueError:
                issues.append("财报日期格式无效")
    return issues


def hard_campaign_issues(campaign: dict, issues: list[str]) -> list[str]:
    if str(campaign.get("trigger_mode", "EARNINGS")).upper() == "EARNINGS":
        return list(dict.fromkeys(issues))
    hard_markers = ("期权代码无法识别", "合约已于")
    return list(dict.fromkeys(
        issue for issue in issues if any(marker in issue for marker in hard_markers)
    ))
