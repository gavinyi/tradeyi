from __future__ import annotations

from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, wait
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .config import normalize_us_symbol
from .earnings_gamma import screen_gamma_contracts


NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"


def _session_from_time(value: object) -> str:
    raw = str(value or "").lower()
    if "before" in raw or "pre-market" in raw or "pre market" in raw:
        return "盘前"
    if "after" in raw or "post-market" in raw or "post market" in raw:
        return "盘后"
    return "待确认"


def _fetch_day(day: date, timeout: float) -> list[dict]:
    url = f"{NASDAQ_URL}?{urlencode({'date': day.isoformat()})}"
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; FutuPaperQuant/1.0)",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/market-activity/earnings",
    })
    last_error: Exception | None = None
    retries = max(0, int(os.getenv("EARNINGS_SOURCE_RETRIES", "1")))
    for _ in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                payload = json.loads(response.read())
            return (((payload.get("data") or {}).get("rows")) or [])
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"{day.isoformat()}: {last_error}")


def fetch_earnings_events(symbols: tuple[str, ...], days: int = 45) -> list[dict]:
    """Fetch Nasdaq dates concurrently with a hard overall deadline."""
    wanted = {symbol.removeprefix("US.").upper() for symbol in symbols}
    start = date.today()
    events: list[dict] = []
    request_timeout = max(1.0, float(os.getenv("EARNINGS_SOURCE_TIMEOUT_SECONDS", "4")))
    overall_timeout = max(5.0, float(os.getenv("EARNINGS_SOURCE_OVERALL_TIMEOUT_SECONDS", "25")))
    workers = min(10, max(1, int(os.getenv("EARNINGS_SOURCE_WORKERS", "6"))))
    dates = [start + timedelta(days=offset) for offset in range(max(1, days))]
    dates = [day for day in dates if day.weekday() < 5]
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="earnings-calendar")
    futures = {executor.submit(_fetch_day, day, request_timeout): day for day in dates}
    done, pending = wait(futures, timeout=overall_timeout)
    for future in pending:
        future.cancel()
    executor.shutdown(wait=False, cancel_futures=True)
    successful = 0
    errors: list[str] = []
    for future in done:
        day = futures[future]
        try:
            rows = future.result()
            successful += 1
        except Exception as exc:
            errors.append(str(exc))
            continue
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if symbol not in wanted:
                continue
            session = _session_from_time(row.get("time"))
            events.append({"symbol": f"US.{symbol}", "event_date": day.isoformat(),
                           "session": session, "source": "Nasdaq Earnings Calendar",
                           "confirmed": session != "待确认"})
    if not successful:
        detail = errors[0] if errors else f"整体超时 {overall_timeout:.0f} 秒"
        raise RuntimeError(f"Nasdaq 财报日历全部请求失败：{detail}")
    return sorted(events, key=lambda item: (item["event_date"], item["symbol"]))


def discover_upcoming_earnings(days: int = 3) -> list[dict]:
    """Discover all Nasdaq earnings in the next calendar days for watchlist additions."""
    start = date.today()
    timeout = max(1.0, float(os.getenv("EARNINGS_SOURCE_TIMEOUT_SECONDS", "4")))
    events: list[dict] = []
    errors: list[str] = []
    for offset in range(max(1, days)):
        day = start + timedelta(days=offset)
        try:
            rows = _fetch_day(day, timeout)
        except Exception as exc:
            errors.append(str(exc))
            continue
        for row in rows:
            try:
                symbol = normalize_us_symbol(str(row.get("symbol", "")))
            except ValueError:
                continue
            session = _session_from_time(row.get("time"))
            events.append({
                "symbol": symbol,
                "name": str(row.get("name", "") or ""),
                "event_date": day.isoformat(),
                "session": session,
                "market_cap": row.get("marketCap", ""),
                "fiscal_quarter": row.get("fiscalQuarterEnding", ""),
                "eps_forecast": row.get("epsForecast", ""),
                "analyst_count": row.get("noOfEsts", ""),
                "source": "Nasdaq Earnings Calendar",
            })
    if not events and errors:
        raise RuntimeError(f"Nasdaq 财报日历请求失败：{errors[0]}")
    unique = {(item["symbol"], item["event_date"]): item for item in events}
    return sorted(unique.values(), key=lambda item: (item["event_date"], item["symbol"]))


def filter_optionable_earnings(
    events: list[dict], broker, max_expiry_days: int = 7,
    min_open_interest: int = 100, max_spread_pct: float = 20.0,
    max_moneyness_pct: float = 10.0,
) -> tuple[list[dict], list[dict]]:
    """Keep earnings symbols with at least one liquid nearby call or put."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    for event in events:
        symbol = str(event["symbol"])
        event_day = date.fromisoformat(str(event["event_date"]))
        earliest = event_day + timedelta(days=1) if event.get("session") == "盘后" else event_day
        latest = event_day + timedelta(days=max(0, max_expiry_days))
        try:
            expirations = broker.option_expirations(symbol).copy()
            expiry_dates = pd.to_datetime(expirations.get("strike_time"), errors="coerce")
            valid = expirations[(expiry_dates.dt.date >= earliest) & (expiry_dates.dt.date <= latest)]
            if valid.empty:
                rejected.append({**event, "filter_reason": "财报后 7 天内没有可用期权到期日"})
                continue
            spot = float(broker.snapshots([symbol]).iloc[0]["last_price"])
            qualified = 0
            nearest_expiration = ""
            for expiration in valid.sort_values("strike_time")["strike_time"].astype(str):
                chain = broker.option_chain(symbol, expiration)
                quotes = broker.snapshots(chain["code"].tolist())
                for option_type in ("CALL", "PUT"):
                    qualified += len(screen_gamma_contracts(
                        chain, quotes, spot, option_type, min_open_interest,
                        max_spread_pct, max_moneyness_pct,
                    ))
                if qualified:
                    nearest_expiration = expiration
                    break
            if not qualified:
                rejected.append({
                    **event,
                    "filter_reason": (
                        f"无同时满足 OI≥{min_open_interest}、价差≤{max_spread_pct:g}%、"
                        f"距离≤{max_moneyness_pct:g}% 且 Bid/Ask 有效的合约"
                    ),
                })
                continue
            accepted.append({
                **event, "qualified_contracts": qualified,
                "nearest_qualified_expiration": nearest_expiration,
            })
        except Exception as exc:
            rejected.append({**event, "filter_reason": f"期权可交易性检查失败：{exc}"})
    return accepted, rejected


def fetch_futu_earnings_events(broker, symbols: tuple[str, ...], days: int = 45) -> list[dict]:
    wanted = set(symbols)
    start, final = date.today(), date.today() + timedelta(days=max(1, days) - 1)
    events = []
    cursor = start
    while cursor <= final:
        end = min(final, cursor + timedelta(days=6))
        frame = broker.earnings_calendar(cursor.isoformat(), end.isoformat())
        for _, row in frame.iterrows():
            symbol = str(row.get("security", ""))
            if symbol not in wanted:
                continue
            pub_type = str(row.get("pub_type", "")).upper().split(".")[-1]
            session = "盘前" if "BEFORE" in pub_type else "盘后" if "AFTER" in pub_type else "待确认"
            events.append({"symbol": symbol, "event_date": str(row["earnings_date"]),
                           "session": session, "source": "Futu Earnings Calendar",
                           "confirmed": session != "待确认"})
        cursor = end + timedelta(days=1)
    return events


def refresh_earnings_events(store, symbols: tuple[str, ...], days: int = 45, broker=None) -> int:
    source_errors = []
    try:
        nasdaq_events = fetch_earnings_events(symbols, days)
    except Exception as exc:
        nasdaq_events = []
        source_errors.append(f"Nasdaq: {exc}")
        store.operational_event("earnings_calendar", "NASDAQ_SOURCE_FAILED", "DEGRADED", {"error": str(exc)})
    try:
        futu_events = fetch_futu_earnings_events(broker, symbols, days) if broker is not None else []
    except Exception as exc:
        futu_events = []
        source_errors.append(f"Futu: {exc}")
        store.operational_event("earnings_calendar", "FUTU_SOURCE_FAILED", "DEGRADED", {"error": str(exc)})
    if not nasdaq_events and not futu_events and source_errors:
        raise RuntimeError("；".join(source_errors))
    nasdaq = {(item["symbol"], item["event_date"]): item for item in nasdaq_events}
    futu = {(item["symbol"], item["event_date"]): item for item in futu_events}
    events = []
    for key in sorted(set(futu) | set(nasdaq)):
        primary, backup = futu.get(key), nasdaq.get(key)
        if primary and backup:
            agreed = primary["session"] == backup["session"] and primary["session"] != "待确认"
            event = dict(primary)
            event["source"] = "Futu + Nasdaq"
            event["confirmed"] = agreed
        else:
            event = dict(primary or backup)
            event["confirmed"] = False
            event["source"] += "（单一来源，待核验）"
        events.append(event)
    for event in events:
        store.upsert_earnings_event(event["symbol"], event["event_date"], event["session"],
                                   event["source"], event["confirmed"])
    return len(events)


def sync_campaign_events(store, campaigns: list[dict]) -> tuple[list[dict], int]:
    """Update existing contract campaigns only; direction/contract selection remains user-owned."""
    events = store.earnings_events(limit=5000)
    by_symbol = {}
    for event in events:
        if event["confirmed"]:
            by_symbol.setdefault(event["symbol"], event)
    updated = 0
    result = []
    for campaign in campaigns:
        item = dict(campaign)
        event = by_symbol.get(item.get("underlying"))
        if event and (item.get("event_date"), item.get("event_session")) != (
            event["event_date"], event["session"]
        ):
            item.update({"event_date": event["event_date"], "event_session": event["session"],
                         "event_source": event["source"], "event_confirmed": True})
            updated += 1
        result.append(item)
    return result, updated
