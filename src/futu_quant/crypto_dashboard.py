from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd


CHINA_TZ = "Asia/Shanghai"


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def china_time_value(value, unit: str | None = None) -> str:
    if value in (None, ""):
        return ""
    if unit:
        timestamp = pd.to_datetime(
            pd.to_numeric(value, errors="coerce"), unit=unit, utc=True, errors="coerce",
        )
    else:
        timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return ""
    return timestamp.tz_convert(CHINA_TZ).isoformat(sep=" ", timespec="seconds")


def localize_crypto_time_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a display copy with known crypto time columns converted to Beijing time."""
    if frame.empty:
        return frame.copy()
    result = frame.copy()
    for column in ("timestamp", "signal_timestamp"):
        if column in result.columns:
            result[column] = result[column].map(lambda value: china_time_value(value, unit="ms"))
    for column in ("date", "created_at", "updated_at", "行情时间", "账本更新时间"):
        if column in result.columns:
            result[column] = result[column].map(china_time_value)
    return result


def format_okx_balance(rows: list[dict]) -> tuple[dict[str, float], pd.DataFrame]:
    account = rows[0] if rows else {}
    details = account.get("details") if isinstance(account.get("details"), list) else []
    summary = {
        "账户总权益": _number(account.get("totalEq")),
        "可用权益": _number(account.get("availEq")),
        "未实现盈亏": _number(account.get("upl")),
    }
    records = []
    for item in details:
        equity = _number(item.get("eq"))
        available = _number(item.get("availBal"))
        frozen = _number(item.get("frozenBal"))
        usd_equity = _number(item.get("eqUsd"))
        if not any((equity, available, frozen, usd_equity)):
            continue
        records.append(
            {
                "币种": item.get("ccy", ""),
                "总余额": equity,
                "可用余额": available,
                "冻结余额": frozen,
                "美元权益": usd_equity,
                "未实现盈亏": _number(item.get("upl")),
            }
        )
    frame = pd.DataFrame(
        records,
        columns=["币种", "总余额", "可用余额", "冻结余额", "美元权益", "未实现盈亏"],
    )
    if not frame.empty:
        frame = frame.sort_values("美元权益", ascending=False).reset_index(drop=True)
    return summary, frame


def format_okx_orders(rows: list[dict]) -> pd.DataFrame:
    records = []
    for item in rows:
        records.append({
            "标的": item.get("instId", ""),
            "方向": {"buy": "买入", "sell": "卖出"}.get(
                str(item.get("side", "")).lower(), item.get("side", "")
            ),
            "订单类型": {
                "limit": "限价单", "market": "市价单", "post_only": "只做挂单",
                "ioc": "立即成交或取消", "fok": "全部成交或取消",
            }.get(str(item.get("ordType", "")).lower(), item.get("ordType", "")),
            "委托数量": _number(item.get("sz")),
            "已成交数量": _number(item.get("accFillSz")),
            "限价": _number(item.get("px")),
            "成交均价": _number(item.get("avgPx")),
            "状态": {
                "live": "待成交", "partially_filled": "部分成交",
                "filled": "全部成交", "canceled": "已撤单",
                "mmp_canceled": "风控撤单",
            }.get(str(item.get("state", "")).lower(), item.get("state", "")),
            "订单号": item.get("ordId", ""),
            "创建时间": china_time_value(item.get("cTime"), unit="ms"),
            "更新时间": china_time_value(item.get("uTime"), unit="ms"),
        })
    return pd.DataFrame(
        records,
        columns=[
            "标的", "方向", "订单类型", "委托数量", "已成交数量",
            "限价", "成交均价", "状态", "订单号", "创建时间", "更新时间",
        ],
    )


def format_okx_swap_positions(rows: list[dict]) -> pd.DataFrame:
    records = []
    for item in rows:
        contracts = _number(item.get("pos"))
        if contracts == 0:
            continue
        mark_price = _number(item.get("markPx"))
        liquidation_price = _number(item.get("liqPx"))
        liquidation_buffer = (
            abs(mark_price - liquidation_price) / mark_price
            if mark_price > 0 and liquidation_price > 0 else None
        )
        updated = pd.to_datetime(
            pd.to_numeric(item.get("uTime"), errors="coerce"),
            unit="ms", utc=True, errors="coerce",
        )
        records.append({
            "标的": item.get("instId", ""),
            "方向": "多仓" if contracts > 0 else "空仓",
            "持仓张数": abs(contracts),
            "开仓均价": _number(item.get("avgPx")),
            "标记价格": mark_price,
            "预估强平价": liquidation_price or None,
            "距强平比例": liquidation_buffer,
            "杠杆": _number(item.get("lever")),
            "保证金": _number(item.get("margin")),
            "保证金率": _number(item.get("mgnRatio")) or None,
            "名义价值": abs(_number(item.get("notionalUsd"))),
            "未实现盈亏": _number(item.get("upl")),
            "收益率": _number(item.get("uplRatio")),
            "已实现盈亏": _number(item.get("realizedPnl")),
            "资金费用": _number(item.get("fundingFee")),
            "保证金模式": {
                "isolated": "逐仓", "cross": "全仓",
            }.get(str(item.get("mgnMode", "")).lower(), item.get("mgnMode", "")),
            "更新时间": (
                updated.tz_convert(CHINA_TZ).isoformat(sep=" ", timespec="seconds")
                if not pd.isna(updated) else ""
            ),
        })
    return pd.DataFrame(records, columns=[
        "合约", "方向", "持仓张数", "开仓均价", "标记价格", "预估强平价",
        "距强平比例", "杠杆", "保证金", "保证金率", "名义价值",
        "未实现盈亏", "收益率", "已实现盈亏", "资金费用",
        "保证金模式", "更新时间",
    ])


def format_okx_fills(rows: list[dict]) -> pd.DataFrame:
    records = []
    for item in rows:
        timestamp = pd.to_datetime(
            pd.to_numeric(item.get("ts"), errors="coerce"),
            unit="ms", utc=True, errors="coerce",
        )
        records.append({
            "合约": item.get("instId", ""),
            "方向": {"buy": "买入", "sell": "卖出"}.get(
                str(item.get("side", "")).lower(), item.get("side", "")
            ),
            "持仓方向": {
                "long": "多仓", "short": "空仓", "net": "净持仓",
            }.get(str(item.get("posSide", "")).lower(), item.get("posSide", "")),
            "成交数量": _number(item.get("fillSz")),
            "成交价格": _number(item.get("fillPx")),
            "手续费": _number(item.get("fee")),
            "手续费币种": item.get("feeCcy", ""),
            "订单号": item.get("ordId", ""),
            "成交号": item.get("tradeId", ""),
            "成交时间": timestamp.tz_convert(CHINA_TZ)
            if not pd.isna(timestamp) else None,
        })
    return pd.DataFrame(records, columns=[
        "标的", "方向", "持仓方向", "成交数量", "成交价格", "手续费",
        "手续费币种", "订单号", "成交号", "成交时间",
    ])


def format_crypto_shadow_holdings(
    allocations: list[dict],
    tickers: dict[str, dict],
    position_state: dict[str, dict] | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    state = position_state or {}
    current_time = now or datetime.now(timezone.utc)
    records = []
    for row in allocations:
        symbol = str(row.get("symbol", ""))
        quantity = _number(row.get("quantity"))
        average_cost = _number(row.get("average_cost"))
        ticker = tickers.get(symbol, {})
        current_price = _number(ticker.get("last"))
        market_value = quantity * current_price
        cost_value = quantity * average_cost
        pnl_amount = market_value - cost_value if current_price > 0 else None
        pnl_ratio = (
            current_price / average_cost - 1
            if current_price > 0 and average_cost > 0 else None
        )
        opened_at = pd.to_datetime(
            state.get(symbol, {}).get("opened_at"), utc=True, errors="coerce",
        )
        held_hours = (
            (current_time - opened_at.to_pydatetime()).total_seconds() / 3600
            if not pd.isna(opened_at) else None
        )
        quote_time = pd.to_datetime(
            pd.to_numeric(ticker.get("ts"), errors="coerce"),
            unit="ms", utc=True, errors="coerce",
        )
        records.append({
            "标的": symbol,
            "当前数量": quantity,
            "开仓均价": average_cost,
            "当前价": current_price if current_price > 0 else None,
            "持仓成本": cost_value,
            "当前市值": market_value if current_price > 0 else None,
            "未实现盈亏": pnl_amount,
            "收益率": pnl_ratio,
            "峰值价": _number(state.get(symbol, {}).get("peak_price")) or None,
            "持有小时": held_hours,
            "行情时间": (
                quote_time.tz_convert(CHINA_TZ).isoformat(sep=" ", timespec="seconds")
                if not pd.isna(quote_time) else ""
            ),
            "账本更新时间": china_time_value(row.get("updated_at", "")),
        })
    return pd.DataFrame(records)


DECISION_LABELS = {
    "BUY": "买入",
    "HOLD": "继续观察",
    "SELL_ALL": "全部卖出",
    "SELL_PARTIAL": "分批止盈",
    "BLOCKED": "已阻塞",
}


def format_crypto_decisions(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["时间", "标的", "决策", "原因"])
    strategy_column = "strategy_id" if "strategy_id" in frame else "strategy"
    if strategy_column in frame:
        frame = frame[frame[strategy_column].eq("crypto_volatility_spot")]
    if frame.empty:
        return pd.DataFrame(columns=["时间", "标的", "决策", "原因"])
    decision_column = "decision" if "decision" in frame else "action"
    result = pd.DataFrame(
        {
            "时间": frame.get("created_at", "").map(china_time_value),
            "标的": frame.get("symbol", ""),
            "决策": frame.get(decision_column, "").map(
                lambda value: DECISION_LABELS.get(str(value), str(value))
            ),
            "原因": frame.get("reason", ""),
        }
    )
    return result.reset_index(drop=True)
