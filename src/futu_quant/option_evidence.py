from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from .option_lifecycle import parse_option_code


@dataclass(frozen=True)
class OptionEvidence:
    status: str
    reason: str
    total_samples: int
    usable_samples: int
    trading_days: int
    missing_samples: int
    missing_days: int
    degraded_ratio: float
    average_spread_pct: float
    first_time: str
    last_time: str


def _prepared(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    if "time_key" not in data:
        return pd.DataFrame()
    data["time_key"] = pd.to_datetime(data["time_key"], errors="coerce")
    for column in ("bid", "ask"):
        data[column] = pd.to_numeric(data.get(column), errors="coerce")
    if "quality_status" not in data:
        data["quality_status"] = "GOOD"
    return data.dropna(subset=["time_key", "bid", "ask"]).sort_values("time_key")


def assess_option_evidence(
    frame: pd.DataFrame, min_samples: int = 5, min_days: int = 1,
) -> OptionEvidence:
    data = _prepared(frame)
    usable_mask = (
        data["bid"].gt(0)
        & data["ask"].ge(data["bid"])
        & data["quality_status"].astype(str).eq("GOOD")
    ) if not data.empty else pd.Series(dtype=bool)
    usable = data[usable_mask].drop_duplicates("time_key") if not data.empty else data
    days = int(usable["time_key"].dt.strftime("%Y-%m-%d").nunique()) if not usable.empty else 0
    usable_count = len(usable)
    missing_samples = max(0, int(min_samples) - usable_count)
    missing_days = max(0, int(min_days) - days)
    degraded_ratio = 1 - usable_count / len(data) if len(data) else 1.0
    if not usable.empty:
        middle = (usable["bid"] + usable["ask"]) / 2
        spreads = ((usable["ask"] - usable["bid"]) / middle.replace(0, pd.NA)).dropna()
        average_spread = float(spreads.mean()) if len(spreads) else 0.0
        first_time = usable["time_key"].iloc[0].isoformat()
        last_time = usable["time_key"].iloc[-1].isoformat()
    else:
        average_spread = 0.0
        first_time = last_time = ""
    if missing_samples or missing_days:
        gaps = []
        if missing_samples:
            gaps.append(f"还缺 {missing_samples} 条可成交盘口")
        if missing_days:
            gaps.append(f"还缺 {missing_days} 个交易日")
        status = "INSUFFICIENT_EVIDENCE"
        reason = "；".join(gaps)
    else:
        status = "SUFFICIENT"
        reason = (
            f"证据充足：{usable_count} 条可成交盘口、{days} 个交易日，"
            f"平均价差 {average_spread:.2%}"
        )
    return OptionEvidence(
        status, reason, len(data), usable_count, days, missing_samples, missing_days,
        float(degraded_ratio), average_spread, first_time, last_time,
    )


def _delta_bucket(value: float) -> str:
    value = abs(value)
    if value < 0.25:
        return "0-0.25"
    if value < 0.40:
        return "0.25-0.40"
    if value <= 0.60:
        return "0.40-0.60"
    if value <= 0.80:
        return "0.60-0.80"
    return "0.80-1.00"


def _dte_bucket(value: int) -> str:
    if value <= 7:
        return "0-7天"
    if value <= 21:
        return "8-21天"
    if value <= 45:
        return "22-45天"
    return "46天以上"


def option_research_buckets(frame: pd.DataFrame, as_of: date | str | None = None) -> pd.DataFrame:
    data = _prepared(frame)
    if data.empty or "code" not in data:
        return pd.DataFrame(columns=["代码", "期权类型", "Delta分组", "到期天数组", "流动性分组", "样本数"])
    reference = pd.Timestamp(as_of or pd.Timestamp.now().date()).date()
    rows = []
    for code, group in data.groupby(data["code"].astype(str)):
        contract = parse_option_code(code)
        if contract is None:
            continue
        delta = float(pd.to_numeric(group.get("delta", pd.Series([0])), errors="coerce").median())
        volume = float(pd.to_numeric(group.get("volume", pd.Series([0])), errors="coerce").median())
        interest = float(pd.to_numeric(group.get("open_interest", pd.Series([0])), errors="coerce").median())
        if volume >= 100 and interest >= 500:
            liquidity = "高"
        elif volume >= 20 or interest >= 100:
            liquidity = "中"
        else:
            liquidity = "低"
        rows.append(
            {
                "代码": code,
                "期权类型": "认购" if contract.option_type == "CALL" else "认沽",
                "Delta分组": _delta_bucket(delta),
                "到期天数组": _dte_bucket(max(0, (contract.expiration - reference).days)),
                "流动性分组": liquidity,
                "样本数": len(group),
            }
        )
    return pd.DataFrame(rows)
