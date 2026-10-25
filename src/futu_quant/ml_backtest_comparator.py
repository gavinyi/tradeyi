"""
ML 增强打分 vs 纯规则打分 对照回测与满足度检验引擎
(ML vs Rule Backtest & Divergence Comparator)

核心目标：
1. 唯一控制变量：同等资金、同等仓位、同等退出线，仅对比排序选品口径；
2. A/B 绩效对决：总收益率、交易胜率、最大回撤、夏普比率、盈亏比并列统计；
3. 五档单调性检验：按分值切分 Q1~Q5 档位，验证高分组是否显著优于低分组；
4. 选品分歧账本：提取 ML 避雷案例（规则看好但 ML 避开亏损）与淘金案例（规则落选但 ML 抓取盈利）；
5. 本次回测对照结论：只描述**单次评估**的差异（不作生产准入判定；准入按
   `data/qa/ml_v32_enable_runbook.md` 的预写阈值，需要时间序重训 + OOT ≥55~60 交易日 + block bootstrap）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .ml_scorer import (
    BuiltinRuleModel,
    MissingFeatureError,
    load_ml_model,
    rule_tiebreak_realised_quality,
)
from .store import TradingStore

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_PATH = ROOT / "data/ml_training/volatility_trades_v3_latest.csv"


@dataclass
class BacktestComparisonResult:
    rule_metrics: dict[str, Any]
    ml_metrics: dict[str, Any]
    equity_series: pd.DataFrame
    quintile_returns: pd.DataFrame
    rank_ic: float
    rule_ic: float
    divergence_records: list[dict[str, Any]]
    readiness_report: dict[str, Any]
    data_quality: dict[str, Any] = field(default_factory=dict)
    tiebreak_quality: dict[str, Any] = field(default_factory=dict)


def run_ml_vs_rule_comparison(
    store: TradingStore | None = None,
    data_path: Path | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    initial_capital: float = 10000.0,
    top_k: int = 3,
    model_path: str | None = None,
) -> BacktestComparisonResult:
    """运行规则 vs ML 的并行回测对照，输出完整分析指标。"""
    file_path = Path(data_path) if data_path else DEFAULT_DATA_PATH
    if not file_path.exists():
        raise FileNotFoundError(f"未找到回测数据源文件: {file_path}")

    df = pd.read_csv(file_path)
    if df.empty:
        raise ValueError("回测数据源为空")

    # 1. 过滤时间窗口
    if "trade_date" in df.columns:
        date_col = "trade_date"
    elif "created_at" in df.columns:
        date_col = "created_at"
    else:
        date_col = None

    if date_col:
        df["_dt_str"] = df[date_col].astype(str).str[:10]
        if start_date:
            df = df[df["_dt_str"] >= str(start_date)[:10]]
        if end_date:
            df = df[df["_dt_str"] <= str(end_date)[:10]]
        df = df.sort_values(by=date_col).reset_index(drop=True)

    if len(df) < 20:
        raise ValueError(f"所选时间窗口内有效样本过少 ({len(df)} 条)，至少需要 20 条样本进行有效比对")

    # 2. 加载两套打分模型
    ml_model, feature_cols = load_ml_model(model_path)
    if hasattr(ml_model, "n_jobs"):
        ml_model.n_jobs = 1
    rule_model = BuiltinRuleModel()

    # 准备特征（P0-B 分层处理，Chief 2026-10-25 裁定）：
    #   * **列缺失 → 抛错指名**：旧实现 `avail_cols` 把缺列静默丢掉，v3.2 模型需要
    #     moneyness/gamma_to_theta 而旧 CSV 从来没有 → ML 臂实际少两个核心特征在打分
    #     （甚至 KeyError 被吞成常数 0.50），大屏 🟢「满足实盘接入」因此不可信；
    #   * **行级 NaN → 不抛**：几行脏数据不许炸掉整个大屏，但要计数 + 原因并回传给页面显示。
    missing_features = [col for col in feature_cols if col not in df.columns]
    if missing_features:
        raise MissingFeatureError(
            f"回测数据缺少模型所需特征 {missing_features}：拒绝用缺列数据打分"
            "（静默丢列会让 ML 臂少特征出分，对照结论无意义）。"
            "请用 scripts/ml_export_training_data_v3.py 重新导出补列。"
        )

    total_rows = len(df)
    missing_by_column = {
        col: int(df[col].isna().sum()) for col in feature_cols if df[col].isna().any()
    }
    usable_mask = df[feature_cols].notna().all(axis=1)
    unusable_rows = int((~usable_mask).sum())
    data_quality = {
        "total_rows": total_rows,
        "usable_rows": total_rows - unusable_rows,
        "unusable_rows": unusable_rows,
        "missing_by_column": missing_by_column,
        "policy": "行级缺失丢弃并计数（列缺失则抛错）；不许静默 fillna(0)",
    }
    if unusable_rows:
        logger.warning(
            "行级缺值丢弃 %s/%s 条：%s", unusable_rows, total_rows, missing_by_column
        )
    df = df[usable_mask].reset_index(drop=True)
    if len(df) < 20:
        raise ValueError(
            f"剔除行级缺值后可用样本过少（剩 {len(df)} 条，丢弃 {unusable_rows} 条）："
            f"{missing_by_column}"
        )
    X = df[feature_cols]

    # 3. 计算双模型预测概率
    try:
        df["ml_score"] = ml_model.predict_proba(X)[:, 1]
    except Exception as exc:  # noqa: BLE001
        # 不再降级成常数 0.50：那会让"模型没打分"与"模型打 0.5 分"无法区分，
        # 大屏却照常出绿 —— 静默降级正是本单要消灭的。
        raise MissingFeatureError(
            f"ML 模型预测失败（{type(exc).__name__}: {exc}）："
            "拒绝降级为常数 0.50，否则对照结论建立在假分数上"
        ) from exc

    df["rule_score"] = rule_model.predict_proba(X)[:, 1]

    # 3.5 兜底 rule 的 tie-break 落地质量监控（Chief 2026-10-25 裁定 ②，双保险）：
    # realised 分位是**单份数据的抽样值**，只入库观察、不作判据；单盐 → K 盐融合后
    # 分散度收窄，但不保证单次落在中位（不允许据此调盐）。
    tiebreak_quality = rule_tiebreak_realised_quality(df)
    if tiebreak_quality.get("status") == "ok":
        logger.info(
            "rule tie-break 落地监控：K=%s 融合 realised 分位 %.1f%%（单盐 %.1f%%）、"
            "%s 组 / %s 对",
            tiebreak_quality.get("fusion_k"),
            tiebreak_quality.get("fused_percentile", float("nan")),
            tiebreak_quality.get("single_salt_percentile", float("nan")),
            tiebreak_quality.get("groups"),
            tiebreak_quality.get("pairs"),
        )
    else:
        logger.warning("rule tie-break 落地监控不可用：%s", tiebreak_quality)
    if store is not None:
        try:
            store.audit(
                "RULE_TIEBREAK_REALISED_QUALITY",
                tiebreak_quality,
                actor="service:ml_comparator",
            )
        except Exception as exc:  # noqa: BLE001
            # 监控写入失败不阻断对照回测（DB 锁等），但必须留痕，不许静默
            logger.warning("rule tie-break 监控审计写入失败（%s）：%s", type(exc).__name__, exc)

    # 实际后验盈亏
    pnl_series = df["window_net_pnl"] if "window_net_pnl" in df.columns else pd.Series(0.0, index=df.index)
    df["actual_pnl"] = pnl_series.fillna(0.0)
    df["is_win"] = df["actual_pnl"] > 0

    # 4. IC / 相关性检验
    ml_ic = float(df["ml_score"].corr(df["actual_pnl"]))
    rule_ic = float(df["rule_score"].corr(df["actual_pnl"]))
    if np.isnan(ml_ic):
        ml_ic = 0.0
    if np.isnan(rule_ic):
        rule_ic = 0.0

    # 5. 五档分层收益分析 (Quintile Analysis)
    # 按 ML 分值切分 Q1 (最高 20%) ~ Q5 (最低 20%)
    try:
        df["quintile"] = pd.qcut(
            df["ml_score"], q=5,
            labels=["Q5 (最低)", "Q4", "Q3", "Q2", "Q1 (最高)"],
            duplicates="drop",
        )
        q_stats = df.groupby("quintile", observed=False).agg(
            avg_pnl=("actual_pnl", "mean"),
            win_rate=("is_win", "mean"),
            count=("actual_pnl", "count"),
        ).reset_index()
    except Exception:
        q_stats = pd.DataFrame(columns=["quintile", "avg_pnl", "win_rate", "count"])

    # 6. A/B 并行组合回测模拟
    # 策略逻辑：按交易日分组，每个时点分别按规则分与 ML 分选取 Top K 标的进行开仓模拟
    group_col = "_dt_str" if "_dt_str" in df.columns else date_col or "symbol"
    dates = df[group_col].unique()

    rule_equity = [initial_capital]
    ml_equity = [initial_capital]
    equity_dates = ["初始"]

    rule_trades_pnl: list[float] = []
    ml_trades_pnl: list[float] = []

    for d in dates:
        day_df = df[df[group_col] == d]
        if day_df.empty:
            continue

        # 规则组选品：按 rule_score 降序取 Top K
        rule_picks = day_df.sort_values(by="rule_score", ascending=False).head(top_k)
        rule_day_pnl = float(rule_picks["actual_pnl"].sum() * 100.0)  # 换算期权乘数
        rule_trades_pnl.extend((rule_picks["actual_pnl"] * 100.0).tolist())
        rule_equity.append(rule_equity[-1] + rule_day_pnl)

        # ML组选品：按 ml_score 降序取 Top K
        ml_picks = day_df.sort_values(by="ml_score", ascending=False).head(top_k)
        ml_day_pnl = float(ml_picks["actual_pnl"].sum() * 100.0)
        ml_trades_pnl.extend((ml_picks["actual_pnl"] * 100.0).tolist())
        ml_equity.append(ml_equity[-1] + ml_day_pnl)

        equity_dates.append(str(d))

    equity_df = pd.DataFrame({
        "日期": equity_dates,
        "规则基准净值": rule_equity,
        "ML增强净值": ml_equity,
    })

    # 计算绩效指标
    def _compute_metrics(trades: list[float], capital_curve: list[float]) -> dict[str, Any]:
        if not trades:
            return {
                "total_return_pct": 0.0, "win_rate": 0.0, "max_drawdown_pct": 0.0,
                "profit_loss_ratio": 0.0, "avg_pnl": 0.0, "trades_count": 0, "sharpe": 0.0,
            }
        trades_arr = np.array(trades)
        wins = trades_arr[trades_arr > 0]
        losses = trades_arr[trades_arr < 0]
        total_ret = (capital_curve[-1] - capital_curve[0]) / capital_curve[0]
        win_rate = len(wins) / len(trades_arr) if len(trades_arr) > 0 else 0.0
        p_l_ratio = (wins.mean() / abs(losses.mean())) if len(wins) > 0 and len(losses) > 0 and losses.mean() != 0 else 1.0

        # 最大回撤
        curve_arr = np.array(capital_curve)
        peak = np.maximum.accumulate(curve_arr)
        drawdowns = (peak - curve_arr) / peak
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

        # 夏普近似 (日收益序列)
        daily_diffs = np.diff(curve_arr) / curve_arr[:-1]
        sharpe = (daily_diffs.mean() / (daily_diffs.std() + 1e-8) * np.sqrt(252)) if len(daily_diffs) > 1 else 0.0

        return {
            "total_return_pct": round(total_ret * 100, 2),
            "win_rate": round(win_rate * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "profit_loss_ratio": round(p_l_ratio, 2),
            "avg_pnl": round(float(trades_arr.mean()), 2),
            "trades_count": len(trades_arr),
            "sharpe": round(float(sharpe), 2),
        }

    rule_metrics = _compute_metrics(rule_trades_pnl, rule_equity)
    ml_metrics = _compute_metrics(ml_trades_pnl, ml_equity)

    # 7. 选品分歧账本 (避雷与淘金)
    divergence_records = []
    # 避雷案例：规则打分高于中位数，但 ML 判定低于中位数且实际亏损
    # 淘金案例：规则打分低于中位数，但 ML 判定高于中位数且实际盈利
    rule_med = df["rule_score"].median()
    ml_med = df["ml_score"].median()

    for _, row in df.iterrows():
        r_sc = float(row["rule_score"])
        m_sc = float(row["ml_score"])
        act_pnl = float(row["actual_pnl"])
        sym = str(row.get("symbol", ""))

        if r_sc >= rule_med and m_sc < ml_med and act_pnl <= 0:
            divergence_records.append({
                "类型": "🛡️ ML避雷案例",
                "标的代码": sym,
                "日期": str(row.get(group_col, "")),
                "规则评分": round(r_sc, 3),
                "ML评分": round(m_sc, 3),
                "分歧差值": round(m_sc - r_sc, 3),
                "实际盈亏": round(act_pnl * 100, 2),
                "归因结果": "规则看好但ML打压，成功规避亏损",
            })
        elif r_sc < rule_med and m_sc >= ml_med and act_pnl > 0:
            divergence_records.append({
                "类型": "🌟 ML淘金案例",
                "标的代码": sym,
                "日期": str(row.get(group_col, "")),
                "规则评分": round(r_sc, 3),
                "ML评分": round(m_sc, 3),
                "分歧差值": round(m_sc - r_sc, 3),
                "实际盈亏": round(act_pnl * 100, 2),
                "归因结果": "规则落选但ML提拔，成功捕获盈利",
            })

    # 8. 生产准入红绿灯评估 (Production Readiness Checklist)
    checks: list[dict[str, Any]] = []
    # 条件 1: 总收益率跑赢规则
    ret_pass = ml_metrics["total_return_pct"] > rule_metrics["total_return_pct"]
    checks.append({
        "项目": "总收益率超额",
        "标准": "ML 总收益率高于规则基准",
        "ML表现": f"{ml_metrics['total_return_pct']}%",
        "规则基准": f"{rule_metrics['total_return_pct']}%",
        "判定": "✅ 达标" if ret_pass else "❌ 未达标",
    })

    # 条件 2: 胜率提升 >= 2%
    win_lift = ml_metrics["win_rate"] - rule_metrics["win_rate"]
    win_pass = win_lift >= 2.0
    checks.append({
        "项目": "交易胜率提升",
        "标准": "胜率相比规则基准提升 ≥ 2.0%",
        "ML表现": f"{ml_metrics['win_rate']}% (提升 {win_lift:+.1f}%)",
        "规则基准": f"{rule_metrics['win_rate']}%",
        "判定": "✅ 达标" if win_pass else "❌ 未达标",
    })

    # 条件 3: 最大回撤不劣化 (回撤不超过规则的 1.1 倍)
    dd_pass = ml_metrics["max_drawdown_pct"] <= max(1.0, rule_metrics["max_drawdown_pct"] * 1.1)
    checks.append({
        "项目": "最大回撤控制",
        "标准": "最大回撤未显著劣于规则组 (≤ 1.1x)",
        "ML表现": f"{ml_metrics['max_drawdown_pct']}%",
        "规则基准": f"{rule_metrics['max_drawdown_pct']}%",
        "判定": "✅ 达标" if dd_pass else "❌ 未达标",
    })

    # 条件 4: 五档单调性检验 (Q1 平均收益高于 Q5)
    if not q_stats.empty and len(q_stats) >= 2:
        q1_row = q_stats[q_stats["quintile"].astype(str).str.contains("Q1")]
        q5_row = q_stats[q_stats["quintile"].astype(str).str.contains("Q5")]
        if not q1_row.empty and not q5_row.empty:
            q1_pnl = float(q1_row["avg_pnl"].values[0])
            q5_pnl = float(q5_row["avg_pnl"].values[0])
            mono_pass = q1_pnl > q5_pnl
            checks.append({
                "项目": "五档单调性检验",
                "标准": "高分层(Q1)实际收益 > 低分层(Q5)实际收益",
                "ML表现": f"Q1=${q1_pnl:+.2f} vs Q5=${q5_pnl:+.2f}",
                "规则基准": "严谨单调性防过拟合",
                "判定": "✅ 达标" if mono_pass else "❌ 未达标",
            })
        else:
            mono_pass = True
    else:
        mono_pass = True

    # 综合判定（**只描述本次回测**，不作准入结论；准入口径见 runbook 预写阈值）
    all_passed = ret_pass and win_pass and dd_pass and mono_pass
    sample_trades = int(len(df))
    sample_days = int(df[group_col].nunique()) if group_col in df.columns else 0
    readiness_report = {
        "is_ready": all_passed,
        "verdict_title": "📊 本次回测结果（单次评估，不构成准入结论）",
        "verdict_desc": (
            f"样本：{sample_trades} 笔 / {sample_days} 个交易日。"
            "本页只展示这一次回测的对照差异：不含置信区间、不做折内重训、无样本量门槛，"
            "因此**只描述本次结果，不作准入判定**（达标/未达标仅是本次四项相对比较）。"
            "准入复测请按 data/qa/ml_v32_enable_runbook.md 的「切/不切判定」小节："
            "预写阈值 + 时间序重训 + OOT ≥55~60 个交易日 + 同口径交易日 block bootstrap。"
        ),
        "evaluation_scope": "single_run_backtest",
        "sample_trades": sample_trades,
        "sample_days": sample_days,
        "admission_reference": "data/qa/ml_v32_enable_runbook.md#切/不切判定",
        "checklist": checks,
    }

    return BacktestComparisonResult(
        rule_metrics=rule_metrics,
        ml_metrics=ml_metrics,
        equity_series=equity_df,
        quintile_returns=q_stats,
        rank_ic=round(ml_ic, 4),
        rule_ic=round(rule_ic, 4),
        divergence_records=divergence_records[:30],
        readiness_report=readiness_report,
        data_quality=data_quality,
        tiebreak_quality=tiebreak_quality,
    )
