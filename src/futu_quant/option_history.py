from __future__ import annotations

from datetime import datetime, timezone
import json

import pandas as pd

from .config import load_all_volatility_campaigns, load_gamma_campaigns
from .data_quality import option_quote_activity, option_quote_freshness
from .option_lifecycle import parse_option_code
from .quote_liveness import BookActivityTracker


# 采集进程内复用：同一合约每轮只观测一次盘口，用「距上次变化秒数」当 `book_active_seconds`。
# ⚠️ 该字段**只做观测**，不参与 `quality_status`（GOOD/DEGRADED 决定 ML 训练样本，不可动）。
_BOOK_ACTIVITY = BookActivityTracker()


def configured_option_contracts(broker=None, store=None) -> tuple[str, ...]:
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
    # 2026-10-27 修复（根因）：引擎每轮「期权表达复核」会把 campaign **roll 成新合约**，
    # 而新 code 不写回 campaigns 配置 → 采集清单看不到它 → 该合约永远没有
    # `option_history` 行 → A14「无历史行即 fail-closed」永远拦住入场
    # （今晚实盘/模拟有 BUY 却零成交、以及 10-22 之后连续 0 单的直接原因）。
    # 修法：把「运行时已选中的活跃合约」并入采集清单（只读 campaign_state，不写键），
    # 下一采集周期起该合约就有盘口观测，A14 有据可依；保护强度不变（仍要求真实观测）。
    if store is not None:
        try:
            for key, value in store.campaign_states().items():
                if not str(key).startswith("volatility_long_gamma"):
                    continue
                active = str((value or {}).get("active_contract_code") or "").upper()
                if parse_option_code(active):
                    codes.add(active)
        except Exception:  # noqa: BLE001
            # silent-ok: 采集清单降级不得打断采集；读不到状态就沿用配置清单
            pass
    if broker is not None:
        positions = broker.positions()
        if not positions.empty and "code" in positions:
            codes.update(
                code for code in positions["code"].astype(str).str.upper()
                if parse_option_code(code)
            )
    return tuple(sorted(codes))


def collect_option_snapshots(broker, store) -> dict:
    contracts = configured_option_contracts(broker, store)
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
    collection_usable: list[bool] = []
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
        book_active_seconds = _BOOK_ACTIVITY.observe(
            code, row, datetime.now(timezone.utc),
        )
        # quality_status 是 **ML 训练标签，口径冻结**（Reviewer 2026-10-27）：
        # 仍按成交时间判（旧口径），不得掺入盘口活性，否则训练样本分布被改。
        fresh, freshness_reason, _, _ = option_quote_freshness(row)
        book_good = bid > 0 and ask >= bid and iv > 0
        quality = "GOOD" if fresh and book_good else "DEGRADED"
        quality_reason = freshness_reason if book_good else (
            f"盘口或 IV 无效：买一 {bid:.3f}、卖一 {ask:.3f}、IV {iv:.4f}"
        )
        # 心跳口径**单独算**（不入库、不影响 quality_status）：成交冻结≠采集失效，
        # 主基准 = min(成交年龄, 盘口变化年龄)。修 2026-10-27 的 4/8 心跳误报。
        collection_fresh, _, _, _ = option_quote_activity(
            row, datetime.fromisoformat(fetched),
            book_active_seconds=book_active_seconds,
            fresh_seconds=180, maximum_seconds=300, maximum_spread_pct=20.0,
        )
        collection_usable.append(bool(collection_fresh and book_good))
        raw_time = row.get("update_time") or row.get("time_key") or fetched
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
    # 心跳用 collection_usable（盘口活性基准），quality_status 仅作 ML 标签统计。
    usable = sum(collection_usable)
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
    # 告警与降级分层治理：
    # 远月或深虚合约由于自然缺乏报价出现部分降级属于市场常态；
    # 只有当合约完全缺失（missing），或整体可用比例低于阈值（如 < 60%）时才判定为 DEGRADED 报警；
    # 若 records 完全为空，绝不可误判为健康（必须判 DEGRADED）。
    usable_ratio = (usable / len(records)) if records else 0.0
    healthy = bool(records) and (usable_ratio >= 0.60) and not missing
    message = (
        f"采集 {len(records)} 个合约；可用 {usable}（{usable_ratio:.1%}），降级 {len(records) - usable}"
        f"（ML 标签 GOOD {good}）"
    )
    if missing:
        message += f"；缺失 {len(missing)}（已记 OPTION_SNAPSHOT_MISSING）"
    store.heartbeat("option_history", "HEALTHY" if healthy else "DEGRADED", message)
    return {
        "contracts": len(contracts), "stored": len(records),
        # good/degraded 保持旧语义（ML 标签），usable 是新心跳口径
        "good": good, "degraded": len(records) - good,
        "usable": usable, "missing": len(missing),
    }


def history_frame(store, code: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    frame = pd.DataFrame(store.option_history(code, start, end))
    if not frame.empty:
        frame["time_key"] = pd.to_datetime(frame["time_key"], errors="coerce")
        frame = frame.dropna(subset=["time_key"]).sort_values("time_key").drop_duplicates("time_key")
    return frame
