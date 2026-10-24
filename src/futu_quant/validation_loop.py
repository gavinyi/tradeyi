from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from zoneinfo import ZoneInfo

import pandas as pd

from .combo_analytics import (
    capacity_assessment, combo_execution_quality, combo_market_snapshot, run_combo_backtest,
)


def market_regime(bars: pd.DataFrame, package_spread: float = 0.0) -> str:
    if package_spread > 0.25:
        return "LIQUIDITY_STRESS"
    if bars.empty or "close" not in bars or len(bars) < 20:
        return "UNKNOWN"
    close = pd.to_numeric(bars["close"], errors="coerce").dropna()
    if len(close) < 20:
        return "UNKNOWN"
    returns = close.pct_change().dropna()
    realized = float(returns.tail(20).std() * math.sqrt(252))
    trend = float(close.iloc[-1] / close.iloc[-20] - 1)
    if realized >= 0.50 or abs(float(returns.iloc[-1])) >= 0.04:
        return "HIGH_VOLATILITY"
    if trend >= 0.05:
        return "TREND_UP"
    if trend <= -0.05:
        return "TREND_DOWN"
    return "RANGE"


def quote_quality(detail: pd.DataFrame) -> dict:
    if detail.empty:
        return {"score": 0.0, "grade": "D", "coverage": 0.0, "reason": "没有行情"}
    valid = (detail["mid"] > 0)
    coverage = float(valid.mean())
    spread_good = float((detail.loc[valid, "spread_pct"] <= 0.15).mean()) if valid.any() else 0.0
    liquidity_good = float(((detail["volume"] >= 20) & (detail["open_interest"] >= 100)).mean())
    freshness_good = float((detail.get("quote_age_seconds", pd.Series(float("inf"), index=detail.index)) <= 180).mean())
    score = 50 * coverage + 20 * spread_good + 15 * liquidity_good + 15 * freshness_good
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
    reasons = []
    if coverage < 1:
        reasons.append(f"双边报价覆盖 {coverage:.0%}")
    if spread_good < 1:
        reasons.append("存在价差超过 15% 的腿")
    if liquidity_good < 1:
        reasons.append("存在成交量或未平仓量不足的腿")
    if freshness_good < 1:
        reasons.append("存在超过 180 秒或无法确认时间的行情")
    return {"score": score, "grade": grade, "coverage": coverage,
            "reason": "；".join(reasons) if reasons else "行情质量良好"}


def collect_combo_market_snapshots(broker, store) -> int:
    count = 0
    for combo in store.option_combos(limit=500):
        if combo["status"] not in {"DRAFT", "EXECUTING", "OPEN", "CLOSING"}:
            continue
        legs = store.option_combo_legs(combo["combo_id"])
        if not legs:
            continue
        quotes = broker.snapshots([leg["symbol"] for leg in legs])
        detail, package = combo_market_snapshot(legs, quotes)
        quality = quote_quality(detail)
        capacity = capacity_assessment(detail)
        try:
            bars = broker.candles(combo["underlying"], count=60)
        except Exception:
            bars = pd.DataFrame()
        regime = market_regime(bars, float(package.get("package_spread", 0)))
        store.record_combo_market_snapshot({
            "combo_id": combo["combo_id"], "underlying": combo["underlying"],
            "regime": regime, "quality_score": quality["score"],
            "quality_grade": quality["grade"], "capacity": capacity.get("capacity", 0),
            **package, "details": {
                "quality": quality, "capacity": capacity,
                "legs": json.loads(detail.to_json(orient="records")),
            },
        })
        count += 1
    return count


def execution_bias(store) -> tuple[pd.DataFrame, dict]:
    intents = pd.DataFrame(store.intents(limit=50_000))
    if intents.empty:
        return pd.DataFrame(), {"status": "INSUFFICIENT", "sample_count": 0}
    intents = intents[pd.to_numeric(intents["filled_quantity"], errors="coerce").fillna(0) > 0].copy()
    if intents.empty:
        return pd.DataFrame(), {"status": "INSUFFICIENT", "sample_count": 0}
    intents["mode"] = intents["intent_key"].astype(str).map(
        lambda value: "SHADOW" if value.endswith(":SHADOW") else "PAPER" if value.endswith(":PAPER") else "UNKNOWN"
    )
    grouped = intents.groupby(["strategy_id", "symbol", "side", "mode"], as_index=False).agg(
        成交均价=("average_fill_price", "mean"), 样本数=("id", "count"),
    )
    shadow = grouped[grouped["mode"] == "SHADOW"].rename(
        columns={"成交均价": "Shadow成交价", "样本数": "Shadow样本"}
    )
    paper = grouped[grouped["mode"] == "PAPER"].rename(
        columns={"成交均价": "模拟盘成交价", "样本数": "模拟盘样本"}
    )
    merged = shadow.merge(paper, on=["strategy_id", "symbol", "side"], how="inner")
    if merged.empty:
        return merged, {"status": "INSUFFICIENT", "sample_count": 0}
    direction = merged["side"].map({"BUY": 1, "SELL": -1})
    merged["不利价格偏差"] = (
        (merged["模拟盘成交价"] / merged["Shadow成交价"] - 1) * direction
    )
    sample_count = int(merged[["Shadow样本", "模拟盘样本"]].min(axis=1).sum())
    return merged, {
        "status": "READY" if sample_count >= 20 else "INSUFFICIENT",
        "sample_count": sample_count,
        "mean_adverse_bias": float(merged["不利价格偏差"].mean()),
    }


def calibrate_scores(observations: pd.DataFrame, minimum: int = 30) -> tuple[pd.DataFrame, dict]:
    required = {"score", "realized_return"}
    if observations.empty or not required.issubset(observations.columns):
        return pd.DataFrame(), {"status": "INSUFFICIENT", "sample_count": 0,
                                "recommendation": "缺少候选评分与已实现收益配对样本"}
    clean = observations.dropna(subset=list(required)).copy()
    if len(clean) < minimum:
        return pd.DataFrame(), {"status": "INSUFFICIENT", "sample_count": len(clean),
                                "recommendation": f"至少需要 {minimum} 个已关闭组合，当前 {len(clean)} 个"}
    clean["评分区间"] = pd.cut(clean["score"], bins=[-math.inf, 40, 60, 80, math.inf], labels=["<40", "40–60", "60–80", "≥80"])
    clean["盈利"] = clean["realized_return"] > 0
    result = clean.groupby("评分区间", observed=True).agg(
        样本数=("score", "count"), 胜率=("盈利", "mean"), 平均收益=("realized_return", "mean"),
    ).reset_index()
    correlation = float(clean[["score", "realized_return"]].corr().iloc[0, 1])
    return result, {"status": "READY", "sample_count": len(clean), "score_return_correlation": correlation,
                    "recommendation": "保持评分权重" if correlation >= 0.2 else "评分区分度不足，建议降低自动交易权重"}


def calibrate_capacity(observations: pd.DataFrame, minimum: int = 20) -> dict:
    required = {"capacity", "requested", "wait_seconds", "slippage", "partial_fill"}
    if observations.empty or not required.issubset(observations.columns) or len(observations) < minimum:
        return {"status": "INSUFFICIENT", "sample_count": len(observations),
                "recommended_volume_participation": 0.05, "recommended_oi_participation": 0.02}
    stressed = observations[
        (observations["wait_seconds"] > 120) | (observations["slippage"] > 0.01)
        | observations["partial_fill"].astype(bool)
    ]
    ratio = len(stressed) / len(observations)
    factor = 0.7 if ratio > 0.30 else 0.85 if ratio > 0.15 else 1.0
    return {"status": "READY", "sample_count": len(observations), "stress_ratio": ratio,
            "recommended_volume_participation": 0.05 * factor,
            "recommended_oi_participation": 0.02 * factor}


def closed_combo_observations(store) -> pd.DataFrame:
    rows = []
    for combo in store.option_combos(limit=5000):
        if combo["status"] != "CLOSED":
            continue
        legs = store.option_combo_legs(combo["combo_id"])
        opening = sum(
            (1 if leg["side"] == "BUY" else -1) * float(leg["filled_quantity"])
            * float(leg["average_fill_price"] or 0) for leg in legs
        )
        closing = 0.0
        for leg in legs:
            intent = store.intent(int(leg["close_intent_id"])) if leg.get("close_intent_id") else None
            if intent:
                closing += (-1 if leg["side"] == "BUY" else 1) * float(intent["filled_quantity"] or 0) * float(intent["average_fill_price"] or 0)
        pnl = -(opening + closing) * 100
        snapshots = store.combo_market_snapshots(combo["combo_id"], limit=1)
        snapshot = snapshots[0] if snapshots else {}
        risk = max(abs(opening) * 100, 1.0)
        rows.append({
            "combo_id": combo["combo_id"], "score": float(snapshot.get("quality_score", 0)),
            "regime": snapshot.get("regime", "UNKNOWN"), "realized_return": pnl / risk,
            "realized_pnl": pnl,
        })
    return pd.DataFrame(rows)


def capacity_observations(store) -> pd.DataFrame:
    rows = []
    for combo in store.option_combos(limit=5000):
        snapshots = store.combo_market_snapshots(combo["combo_id"], limit=1)
        capacity = int(snapshots[0]["capacity"]) if snapshots else 0
        for leg in store.option_combo_legs(combo["combo_id"]):
            if not leg.get("intent_id"):
                continue
            intent = store.intent(int(leg["intent_id"]))
            if not intent:
                continue
            created = datetime.fromisoformat(intent["created_at"])
            updated = datetime.fromisoformat(intent["updated_at"])
            reference = float(intent["limit_price"] or 0)
            average = float(intent["average_fill_price"] or 0)
            direction = 1 if intent["side"] == "BUY" else -1
            rows.append({
                "capacity": capacity, "requested": int(intent["quantity"]),
                "wait_seconds": (updated - created).total_seconds(),
                "slippage": ((average / reference - 1) * direction) if reference and average else 0,
                "partial_fill": 0 < float(intent["filled_quantity"] or 0) < float(intent["quantity"]),
            })
    return pd.DataFrame(rows)


def regime_performance(observations: pd.DataFrame) -> pd.DataFrame:
    if observations.empty or not {"regime", "realized_return"}.issubset(observations.columns):
        return pd.DataFrame()
    values = observations.copy()
    values["盈利"] = values["realized_return"] > 0
    return values.groupby("regime", as_index=False).agg(
        样本数=("combo_id", "count"), 胜率=("盈利", "mean"),
        平均收益=("realized_return", "mean"), 最差收益=("realized_return", "min"),
    )


def strategy_drift(store, minimum: int = 20) -> pd.DataFrame:
    frame = pd.DataFrame(store.intents(limit=50_000))
    if frame.empty:
        return pd.DataFrame()
    frame["created_at"] = pd.to_datetime(frame["created_at"], utc=True)
    frame["mode"] = frame["intent_key"].astype(str).map(
        lambda value: "PAPER" if value.endswith(":PAPER") else "SHADOW" if value.endswith(":SHADOW") else "UNKNOWN"
    )
    rows = []
    for strategy, group in frame.sort_values("created_at").groupby("strategy_id"):
        paper = group[group["mode"] == "PAPER"]
        group = paper if len(paper) >= minimum else group.iloc[0:0]
        if len(group) < minimum:
            rows.append({"策略": strategy, "状态": "样本不足", "样本数": len(group), "建议": f"至少需要 {minimum} 笔"})
            continue
        split = max(10, int(len(group) * 0.7))
        baseline, recent = group.iloc[:split], group.iloc[split:]
        if recent.empty:
            continue
        def metrics(part):
            filled = pd.to_numeric(part["filled_quantity"], errors="coerce").fillna(0) > 0
            failed = part["status"].isin(["FAILED", "REJECTED", "UNKNOWN"])
            reference = pd.to_numeric(part["limit_price"], errors="coerce").replace(0, pd.NA)
            average = pd.to_numeric(part["average_fill_price"], errors="coerce")
            direction = part["side"].map({"BUY": 1, "SELL": -1})
            slip = ((average / reference - 1) * direction)[filled].dropna()
            return float(filled.mean()), float(failed.mean()), float(slip.mean()) if not slip.empty else 0.0
        base_fill, base_fail, base_slip = metrics(baseline)
        recent_fill, recent_fail, recent_slip = metrics(recent)
        drifted = recent_fill < base_fill - 0.20 or recent_fail > base_fail + 0.15 or recent_slip > max(0.01, base_slip + 0.005)
        rows.append({
            "策略": strategy, "状态": "显著走样" if drifted else "稳定", "样本数": len(group),
            "基准成交率": base_fill, "近期成交率": recent_fill,
            "基准失败率": base_fail, "近期失败率": recent_fail,
            "基准滑点": base_slip, "近期滑点": recent_slip,
            "建议": "自动降为 SHADOW" if drifted else "保持当前模式",
        })
    return pd.DataFrame(rows)


def apply_auto_downgrade(store, drift: pd.DataFrame) -> list[str]:
    if store.get_setting("validation_auto_downgrade", "false") != "true" or drift.empty:
        return []
    changed = []
    for _, row in drift.iterrows():
        strategy = str(row["策略"])
        if row["状态"] != "显著走样":
            continue
        global_mode = store.get_setting("execution_mode", "PAPER").upper()
        current = (store.get_setting(f"strategy_execution_mode:{strategy}", "") or global_mode).upper()
        if current != "PAPER":
            continue
        store.set_setting(f"strategy_execution_mode:{strategy}", "SHADOW")
        store.audit("VALIDATION_AUTO_DOWNGRADE", {"strategy": strategy, "from": "PAPER", "to": "SHADOW"}, strategy, actor="service:validation")
        changed.append(strategy)
    return changed


def combo_walk_forward(
    histories: dict[str, pd.DataFrame], legs: list[dict], windows: int = 4,
    initial_cash: float = 100_000,
) -> tuple[pd.DataFrame, dict]:
    common_start = max(pd.to_datetime(frame["time_key"], utc=True).min() for frame in histories.values())
    common_end = min(pd.to_datetime(frame["time_key"], utc=True).max() for frame in histories.values())
    if pd.isna(common_start) or pd.isna(common_end) or common_end <= common_start:
        raise ValueError("组合腿没有共同历史区间")
    boundaries = pd.date_range(common_start, common_end, periods=windows + 1)
    rows = []
    for index in range(windows):
        sliced = {}
        for symbol, frame in histories.items():
            values = frame.copy()
            values["time_key"] = pd.to_datetime(values["time_key"], utc=True)
            sliced[symbol] = values[(values["time_key"] >= boundaries[index]) & (values["time_key"] <= boundaries[index + 1])]
        try:
            _, _, metrics = run_combo_backtest(sliced, legs, initial_cash)
            rows.append({"窗口": index + 1, "开始": boundaries[index], "结束": boundaries[index + 1],
                         "收益率": metrics["总收益率"], "最大回撤": metrics["最大回撤"], "样本数": metrics["样本数"]})
        except ValueError:
            rows.append({"窗口": index + 1, "开始": boundaries[index], "结束": boundaries[index + 1],
                         "收益率": pd.NA, "最大回撤": pd.NA, "样本数": 0})
    result = pd.DataFrame(rows)
    valid = result.dropna(subset=["收益率"])
    if len(valid) < max(2, windows // 2):
        return result, {"status": "INSUFFICIENT", "valid_windows": len(valid), "total_windows": windows}
    return result, {"status": "READY", "valid_windows": len(valid), "total_windows": windows,
                    "profitable_window_ratio": float((valid["收益率"] > 0).mean()),
                    "median_return": float(valid["收益率"].median()),
                    "worst_drawdown": float(valid["最大回撤"].min())}


def validation_report(store, period_type: str = "DAILY") -> tuple[str, dict]:
    now = datetime.now(timezone.utc)
    china_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    snapshots = pd.DataFrame(store.combo_market_snapshots(limit=10_000))
    calibrations = pd.DataFrame(store.calibration_runs(limit=1000))
    cutoff = now - timedelta(days=1 if period_type == "DAILY" else 7)
    if not snapshots.empty:
        snapshots = snapshots[pd.to_datetime(snapshots["created_at"], utc=True) >= cutoff]
    if not calibrations.empty:
        calibrations = calibrations[pd.to_datetime(calibrations["created_at"], utc=True) >= cutoff]
    drift = strategy_drift(store)
    quality = float(snapshots["quality_score"].mean()) if not snapshots.empty else 0.0
    drifted = int((drift.get("状态") == "显著走样").sum()) if not drift.empty else 0
    insufficient = int((drift.get("状态") == "样本不足").sum()) if not drift.empty else 0
    status = "WARNING" if drifted else "INSUFFICIENT" if snapshots.empty else "HEALTHY"
    summary = {
        "combo_snapshot_count": len(snapshots), "average_quality_score": quality,
        "drifted_strategies": drifted, "insufficient_strategies": insufficient,
        "calibration_runs": len(calibrations), "status": status,
    }
    markdown = (
        f"# {'每日' if period_type == 'DAILY' else '每周'}策略验证报告\n\n"
        f"生成时间：{china_now:%Y-%m-%d %H:%M 中国标准时间}\n\n"
        f"- 总体状态：{status}\n- 组合行情快照：{len(snapshots)} 条\n"
        f"- 平均数据质量：{quality:.1f}/100\n- 显著走样策略：{drifted} 个\n"
        f"- 样本不足策略：{insufficient} 个\n- 校准运行：{len(calibrations)} 次\n\n"
        "说明：样本不足不会被视为通过；自动治理只允许 PAPER 降级为 SHADOW。\n"
    )
    key = now.date().isoformat() if period_type == "DAILY" else f"{now:%G-W%V}"
    store.save_validation_report(period_type, key, status, summary, markdown)
    return markdown, summary


def run_validation_cycle(broker, store) -> dict:
    snapshots = collect_combo_market_snapshots(broker, store)
    bias_frame, bias = execution_bias(store)
    store.record_calibration(
        "EXECUTION_BIAS", bias["status"], bias["sample_count"], bias,
        "继续积累 Shadow/PAPER 配对成交" if bias["status"] != "READY" else "使用实测不利偏差修正回测滑点",
    )
    quality = combo_execution_quality(store)
    closed = closed_combo_observations(store)
    score_table, score = calibrate_scores(closed)
    store.record_calibration(
        "CANDIDATE_SCORE", score["status"], score["sample_count"], score,
        score["recommendation"],
    )
    capacity = calibrate_capacity(capacity_observations(store))
    store.record_calibration(
        "CAPACITY", capacity["status"], capacity["sample_count"], capacity,
        "样本不足，保持成交量 5%、未平仓量 2% 的保守参与率" if capacity["status"] != "READY" else "按推荐参与率更新容量模型",
    )
    drift = strategy_drift(store)
    downgraded = apply_auto_downgrade(store, drift)
    for _, row in drift.iterrows():
        store.record_calibration(
            "STRATEGY_DRIFT", "WARNING" if row["状态"] == "显著走样" else row["状态"],
            int(row["样本数"]), row.dropna().to_dict(), str(row["建议"]), str(row["策略"]),
        )
    regimes = regime_performance(closed)
    store.record_calibration(
        "MARKET_REGIME", "READY" if len(closed) >= 20 else "INSUFFICIENT", len(closed),
        {"rows": regimes.to_dict("records")},
        "按市场状态保持独立统计，至少 20 个已关闭组合后用于自动治理",
    )
    return {"snapshots": snapshots, "bias_pairs": len(bias_frame), "drift_rows": len(drift),
            "downgraded": downgraded, "quality_rows": len(quality),
            "score_rows": len(score_table), "regime_rows": len(regimes)}
