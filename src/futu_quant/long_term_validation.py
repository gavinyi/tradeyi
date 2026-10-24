from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import json

import pandas as pd

from .validation_loop import (
    apply_auto_downgrade, calibrate_capacity, capacity_observations,
    closed_combo_observations, calibrate_scores, regime_performance, strategy_drift,
)


HORIZONS = (7, 30, 90)


def equity_horizon_metrics(store, horizons: tuple[int, ...] = HORIZONS) -> pd.DataFrame:
    frame = pd.DataFrame(store.equity_history())
    if frame.empty:
        return pd.DataFrame(columns=["周期", "样本数", "收益率", "最大回撤", "年化波动", "状态"])
    frame["created_at"] = pd.to_datetime(frame["created_at"], utc=True)
    frame["total_assets"] = pd.to_numeric(frame["total_assets"], errors="coerce")
    now = datetime.now(timezone.utc)
    rows = []
    for days in horizons:
        sample = frame[frame["created_at"] >= now - timedelta(days=days)].dropna(subset=["total_assets"])
        if len(sample) < 2:
            rows.append({"周期": f"{days}天", "样本数": len(sample), "收益率": 0.0,
                         "最大回撤": 0.0, "年化波动": 0.0, "状态": "样本不足"})
            continue
        returns = sample["total_assets"].pct_change().dropna()
        drawdown = sample["total_assets"] / sample["total_assets"].cummax() - 1
        elapsed = max(1.0, (sample["created_at"].iloc[-1] - sample["created_at"].iloc[0]).total_seconds() / 86400)
        rows.append({"周期": f"{days}天", "样本数": len(sample),
                     "收益率": float(sample["total_assets"].iloc[-1] / sample["total_assets"].iloc[0] - 1),
                     "最大回撤": float(drawdown.min()),
                     "年化波动": float(returns.std() * (365 / elapsed * len(returns)) ** 0.5) if len(returns) > 1 else 0.0,
                     "状态": "有效"})
    return pd.DataFrame(rows)


def realized_trade_ledger(store) -> pd.DataFrame:
    intents = pd.DataFrame(store.intents(limit=50_000))
    if intents.empty:
        return pd.DataFrame()
    intents = intents[pd.to_numeric(intents["filled_quantity"], errors="coerce").fillna(0) > 0].copy()
    if intents.empty:
        return pd.DataFrame()
    intents["created_at"] = pd.to_datetime(intents["created_at"], utc=True)
    lots: dict[tuple[str, str], deque] = defaultdict(deque)
    rows = []
    for _, fill in intents.sort_values("created_at").iterrows():
        key = (str(fill["strategy_id"]), str(fill["symbol"]))
        metadata = json.loads(fill.get("metadata_json", "{}") or "{}")
        multiplier = float(metadata.get("multiplier", 100 if _looks_like_option(key[1]) else 1))
        quantity = float(fill["filled_quantity"])
        price = float(fill["average_fill_price"] or 0)
        fee = float(fill.get("fees", 0) or 0)
        reference = float(metadata.get("reference_price", fill["limit_price"]) or fill["limit_price"])
        adverse_slippage = (price - reference) * (1 if fill["side"] == "BUY" else -1) * quantity * multiplier
        if fill["side"] == "BUY":
            lots[key].append([quantity, price, multiplier, fee, adverse_slippage])
            continue
        remaining, pnl, allocated_fee, allocated_slippage = quantity, 0.0, fee, adverse_slippage
        while remaining > 0 and lots[key]:
            lot_qty, lot_price, lot_multiplier, lot_fee, lot_slippage = lots[key][0]
            matched = min(remaining, lot_qty)
            fraction = matched / lot_qty if lot_qty else 0
            pnl += (price - lot_price) * matched * lot_multiplier
            allocated_fee += lot_fee * fraction
            allocated_slippage += lot_slippage * fraction
            remaining -= matched
            lot_qty -= matched
            if lot_qty <= 1e-12:
                lots[key].popleft()
            else:
                lots[key][0] = [lot_qty, lot_price, lot_multiplier,
                                lot_fee * (1 - fraction), lot_slippage * (1 - fraction)]
        rows.append({"策略": key[0], "代码": key[1], "平仓时间": fill["created_at"],
                     "已实现毛盈亏": pnl, "手续费": allocated_fee,
                     "不利滑点成本": allocated_slippage,
                     "净盈亏": pnl - allocated_fee, "盈利": pnl - allocated_fee > 0})
    return pd.DataFrame(rows)


def _looks_like_option(symbol: str) -> bool:
    return symbol.startswith("US.") and any(marker in symbol for marker in ("C", "P")) and any(char.isdigit() for char in symbol)


def strategy_horizon_metrics(store, horizons: tuple[int, ...] = HORIZONS) -> pd.DataFrame:
    ledger = realized_trade_ledger(store)
    strategies = sorted({str(row["strategy_id"]) for row in store.intents(limit=50_000)})
    now = datetime.now(timezone.utc)
    rows = []
    for strategy in strategies:
        for days in horizons:
            sample = ledger[(ledger["策略"] == strategy) & (ledger["平仓时间"] >= now - timedelta(days=days))] if not ledger.empty else pd.DataFrame()
            rows.append({"策略": strategy, "周期": f"{days}天", "已平仓交易": len(sample),
                         "胜率": float(sample["盈利"].mean()) if not sample.empty else 0.0,
                         "已实现净盈亏": float(sample["净盈亏"].sum()) if not sample.empty else 0.0,
                         "手续费": float(sample["手续费"].sum()) if not sample.empty else 0.0,
                         "不利滑点成本": float(sample["不利滑点成本"].sum()) if not sample.empty else 0.0,
                         "状态": "有效" if len(sample) >= 5 else "样本不足"})
    return pd.DataFrame(rows)


def stratified_execution_quality(store) -> pd.DataFrame:
    frame = pd.DataFrame(store.intents(limit=50_000))
    if frame.empty:
        return pd.DataFrame()
    frame = frame[pd.to_numeric(frame["filled_quantity"], errors="coerce").fillna(0) > 0].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["时间"] = pd.to_datetime(frame["created_at"], utc=True).dt.tz_convert("America/New_York")
    frame["时段"] = frame["时间"].dt.hour.map(lambda hour: "开盘" if hour < 11 else "尾盘" if hour >= 15 else "盘中")
    frame["模式"] = frame["intent_key"].astype(str).map(lambda value: "SHADOW" if value.endswith(":SHADOW") else "PAPER")
    references = frame.apply(lambda row: float(json.loads(row.get("metadata_json", "{}") or "{}").get("reference_price", row["limit_price"]) or row["limit_price"]), axis=1)
    direction = frame["side"].map({"BUY": 1, "SELL": -1})
    frame["不利滑点"] = (pd.to_numeric(frame["average_fill_price"], errors="coerce") / references.replace(0, pd.NA) - 1) * direction
    return frame.groupby(["strategy_id", "symbol", "时段", "模式"], as_index=False).agg(
        样本数=("id", "count"), 平均不利滑点=("不利滑点", "mean"),
        最大不利滑点=("不利滑点", "max"), 平均成交数量=("filled_quantity", "mean"),
    ).rename(columns={"strategy_id": "策略", "symbol": "代码"})


def stored_greeks_attribution(store) -> pd.DataFrame:
    combos = {row["combo_id"]: row for row in store.option_combos(limit=5000)}
    latest: dict[str, dict] = {}
    for row in store.combo_market_snapshots(limit=20_000):
        latest.setdefault(row["combo_id"], row)
    rows = []
    for combo_id, snapshot in latest.items():
        combo = combos.get(combo_id)
        if not combo or combo["status"] not in {"EXECUTING", "OPEN", "CLOSING"}:
            continue
        rows.append({"策略": combo["strategy_id"], "正股": combo["underlying"], "组合": combo_id,
                     "Delta": float(snapshot.get("net_delta") or 0),
                     "Gamma": float(snapshot.get("net_gamma") or 0),
                     "Theta": float(snapshot.get("net_theta") or 0),
                     "Vega": float(snapshot.get("net_vega") or 0),
                     "容量": int(snapshot.get("capacity") or 0),
                     "行情质量": float(snapshot.get("quality_score") or 0),
                     "市场状态": snapshot.get("regime", "UNKNOWN")})
    return pd.DataFrame(rows)


def stability_scores(store) -> pd.DataFrame:
    frame = pd.DataFrame(store.intents(limit=50_000))
    drift = strategy_drift(store).set_index("策略") if not strategy_drift(store).empty else pd.DataFrame()
    if frame.empty:
        return pd.DataFrame()
    rows = []
    for strategy, group in frame.groupby("strategy_id"):
        samples = len(group)
        filled = pd.to_numeric(group["filled_quantity"], errors="coerce").fillna(0) > 0
        reject = group["status"].isin(["REJECTED", "FAILED", "UNKNOWN"])
        fill_rate, failure_rate = float(filled.mean()), float(reject.mean())
        sample_score = min(25.0, samples / 20 * 25)
        execution_score = max(0.0, 35 * min(1.0, fill_rate / 0.7))
        failure_score = max(0.0, 25 * (1 - failure_rate / 0.2))
        drifted = not drift.empty and strategy in drift.index and drift.loc[strategy, "状态"] == "显著走样"
        score = sample_score + execution_score + failure_score + (0 if drifted else 15)
        rows.append({"策略": strategy, "稳定性评分": round(score, 1),
                     "等级": "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D",
                     "样本数": samples, "成交率": fill_rate, "失败拒绝率": failure_rate,
                     "参数走样": "是" if drifted else "否",
                     "建议": "保持 PAPER" if score >= 70 and not drifted else "保持/降级 SHADOW"})
    return pd.DataFrame(rows).sort_values("稳定性评分", ascending=False)


def calibration_summary(store) -> dict:
    closed = closed_combo_observations(store)
    score_table, score = calibrate_scores(closed)
    capacity = calibrate_capacity(capacity_observations(store))
    regimes = regime_performance(closed)
    return {"candidate_score": score, "candidate_buckets": score_table.to_dict("records"),
            "capacity": capacity, "regimes": regimes.to_dict("records")}


def long_term_validation_cycle(store) -> dict:
    stability = stability_scores(store)
    drift = strategy_drift(store)
    downgraded = apply_auto_downgrade(store, drift)
    result = {"equity": equity_horizon_metrics(store).to_dict("records"),
              "strategy": strategy_horizon_metrics(store).to_dict("records"),
              "stability": stability.to_dict("records"),
              "execution_layers": stratified_execution_quality(store).to_dict("records"),
              "greeks": stored_greeks_attribution(store).to_dict("records"),
              "calibration": calibration_summary(store), "downgraded": downgraded,
              "governance": "只允许 PAPER 自动降级到 SHADOW；恢复 PAPER 必须管理员人工确认"}
    status = "WARNING" if downgraded or (not stability.empty and stability["等级"].isin(["D"]).any()) else "HEALTHY"
    store.operational_event("long_term_validation", "LONG_TERM_VALIDATION", status, result)
    return result


def manually_restore_paper(store, strategy_id: str, actor: str, reason: str) -> None:
    if not reason.strip():
        raise ValueError("人工恢复必须填写原因")
    current = store.get_setting(f"strategy_execution_mode:{strategy_id}", "").upper()
    if current != "SHADOW":
        raise ValueError("只有已降级为 SHADOW 的策略可以人工恢复")
    store.set_setting(f"strategy_execution_mode:{strategy_id}", "PAPER")
    store.audit("VALIDATION_MANUAL_RESTORE", {"strategy": strategy_id, "actor": actor,
                                               "from": "SHADOW", "to": "PAPER", "reason": reason}, strategy_id, actor="service:validation")
