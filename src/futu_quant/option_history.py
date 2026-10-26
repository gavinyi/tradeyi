from __future__ import annotations

from datetime import datetime, timezone
import json

import pandas as pd

from .config import load_all_volatility_campaigns, load_gamma_campaigns
from .data_quality import option_quote_freshness
from .option_lifecycle import parse_option_code
from .quote_liveness import BookActivityTracker


# 采集进程内复用：同一合约每轮只观测一次盘口，用「距上次变化秒数」当 `book_active_seconds`。
# ⚠️ 该字段**只做观测**，不参与 `quality_status`（GOOD/DEGRADED 决定 ML 训练样本，不可动）。
_BOOK_ACTIVITY = BookActivityTracker()


def configured_option_contracts(broker=None) -> tuple[str, ...]:
    # #1 键级分区：**故意读全局**（含各租户键）—— 期权历史是回测/研究数据源，
    # 本就该看全量合约；且这里**只读**，不参与清理、不写键。
    codes: set[str] = set()
    for campaign in (*load_gamma_campaigns(), *load_all_volatility_campaigns()):
        direct = str(campaign.get("code", "")).upper()
        if parse_option_code(direct):
            codes.add(direct)
        legs = campaign.get("legs", [])
        if isinstance(legs, str):
            try:
                legs = json.loads(legs)
            except json.JSONDecodeError:
                legs = []
        for leg in legs or []:
            code = str(leg.get("code", "")).upper()
            if parse_option_code(code):
                codes.add(code)
    if broker is not None:
        positions = broker.positions()
        if not positions.empty and "code" in positions:
            codes.update(
                code for code in positions["code"].astype(str).str.upper()
                if parse_option_code(code)
            )
    return tuple(sorted(codes))


def collect_option_snapshots(broker, store) -> dict:
    contracts = configured_option_contracts(broker)
    if not contracts:
        return {"contracts": 0, "stored": 0, "good": 0, "degraded": 0}
    parsed = {code: parse_option_code(code) for code in contracts}
    underlyings = sorted({item.underlying for item in parsed.values() if item})
    frame = broker.snapshots([*contracts, *underlyings])
    if frame.empty or "code" not in frame:
        raise ValueError("期权历史采集没有取得行情快照")
    indexed = {str(row["code"]): row for _, row in frame.iterrows()}
    fetched = datetime.now(timezone.utc).isoformat()
    records: list[dict] = []
    missing: list[dict] = []
    for code, contract in parsed.items():
        row = indexed.get(code)
        underlying_row = indexed.get(contract.underlying) if contract else None
        if row is None or contract is None:
            # 缺合约不再静默跳过（2026-10-26 静默降级清零）：
            # 记入 missing，落审计 + 心跳降级，禁止只留 continue。
            missing.append({
                "code": code,
                "reason": "contract_unparsable" if contract is None else "snapshot_absent",
            })
            continue
        bid = float(row.get("bid_price", 0) or 0)
        ask = float(row.get("ask_price", 0) or 0)
        iv = float(row.get("option_implied_volatility", 0) or 0)
        fresh, freshness_reason, _, _ = option_quote_freshness(row)
        book_good = bid > 0 and ask >= bid and iv > 0
        quality = "GOOD" if fresh and book_good else "DEGRADED"
        quality_reason = freshness_reason if book_good else (
            f"盘口或 IV 无效：买一 {bid:.3f}、卖一 {ask:.3f}、IV {iv:.4f}"
        )
        raw_time = row.get("update_time") or row.get("time_key") or fetched
        book_active_seconds = _BOOK_ACTIVITY.observe(
            code, row, datetime.now(timezone.utc),
        )
        records.append({
            "code": code, "time_key": str(raw_time), "underlying": contract.underlying,
            "underlying_price": float(underlying_row.get("last_price", 0) or 0)
            if underlying_row is not None else 0,
            "bid": bid, "ask": ask, "iv": iv,
            "delta": float(row.get("option_delta", 0) or 0),
            "gamma": float(row.get("option_gamma", 0) or 0),
            "theta": float(row.get("option_theta", 0) or 0),
            "vega": float(row.get("option_vega", 0) or 0),
            "rho": float(row.get("option_rho", 0) or 0),
            "volume": float(row.get("volume", 0) or 0),
            "open_interest": float(row.get("option_open_interest", 0) or 0),
            "source": "FUTU_SNAPSHOT_V1", "fetched_at": fetched,
            "quality_status": quality, "quality_reason": quality_reason,
            "book_active_seconds": book_active_seconds,
        })
    store.import_option_history(records)
    good = sum(record["quality_status"] == "GOOD" for record in records)
    if missing:
        reasons: dict[str, int] = {}
        for item in missing:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        store.audit(
            "OPTION_SNAPSHOT_MISSING",
            {
                "missing_count": len(missing),
                "expected_count": len(parsed),
                "contracts": [item["code"] for item in missing][:20],
                "reasons": reasons,
            },
            "option_history",
            actor="service:option_history",
        )
    healthy = good == len(records) and not missing
    message = f"采集 {len(records)} 个合约；可用 {good}，降级 {len(records) - good}"
    if missing:
        message += f"；缺失 {len(missing)}（已记 OPTION_SNAPSHOT_MISSING）"
    store.heartbeat("option_history", "HEALTHY" if healthy else "DEGRADED", message)
    return {
        "contracts": len(contracts), "stored": len(records),
        "good": good, "degraded": len(records) - good, "missing": len(missing),
    }


def history_frame(store, code: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    frame = pd.DataFrame(store.option_history(code, start, end))
    if not frame.empty:
        frame["time_key"] = pd.to_datetime(frame["time_key"], errors="coerce")
        frame = frame.dropna(subset=["time_key"]).sort_values("time_key").drop_duplicates("time_key")
    return frame
