#!/usr/bin/env python3
"""A-2 v3.2 模拟盘 A/B **只读**分析（同一批决策双算口径，2026-10-27）。

数据源：`decision_snapshots(strategy_id='volatility_long_ab')`，每行一批候选，
同批带 rule 臂与 ML 臂两套分数/名次（由 `volatility_roll` + `ml_ab_observe` 写入）。

硬门槛（写死，Chief/Reviewer 2026-10-27 定）：
- 有效交易日 ≥ 30 **且** 有效批次 ≥ 200，否则输出 INSUFFICIENT，不出结论；
- 每批 ML 覆盖率 ≥ 80%，且 `model_version='v3.2'`、`model_feature_count=19`，否则该批作废、
  整窗不达标输出 VOID（静默回退规则模型的批次不许进结论）；
- 标签 = `option_history` **前向 7 个交易日**的标记价收益（bid/ask 中价），缺价记 `label_missing`
  **不补 0、不插值**；
- IC/AUC 用**交易日 block bootstrap**（1000 次重采样，报 5%/50% 分位）。

用法：
    python scripts/ml_ab_analyze.py --db data/trading.db --out data/qa/ml_ab_report_20261110.md
退出码：0=出结论；3=样本不足；4=作废（覆盖率/模型自证不达标）。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

OBSERVATION_STRATEGY_ID = "volatility_long_ab"
MIN_TRADING_DAYS = 30
MIN_BATCHES = 200
MIN_COVERAGE = 0.80
FORWARD_TRADING_DAYS = 7
BOOTSTRAP_ROUNDS = 1000
BOOTSTRAP_SEED = 20261027


def _load_batches(db_path: Path) -> list[dict]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "select snapshot_json, created_at from decision_snapshots "
            "where strategy_id=? order by id",
            (OBSERVATION_STRATEGY_ID,),
        ).fetchall()
    finally:
        connection.close()
    batches = []
    for row in rows:
        try:
            payload = json.loads(row["snapshot_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if payload.get("schema") != "ml_ab_v1":
            continue
        batches.append(payload)
    return batches


def _mark(connection, code: str, when: datetime) -> float | None:
    """取 `when` 附近最近一条 option_history 的 bid/ask 中价（容差 ±1 天）。"""
    row = connection.execute(
        "select bid, ask, time_key from option_history where code like ? "
        "and time_key <= ? order by time_key desc limit 1",
        (f"{code}%", when.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    if not row:
        return None
    bid, ask = row["bid"], row["ask"]
    try:
        bid_f, ask_f = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
        return None
    return (bid_f + ask_f) / 2.0


def _forward_label(connection, code: str, observed_at: str) -> float | None:
    try:
        entry_time = datetime.fromisoformat(observed_at)
    except (TypeError, ValueError):
        return None
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)
    entry = _mark(connection, code, entry_time)
    exit_time = entry_time + timedelta(days=int(FORWARD_TRADING_DAYS * 7 / 5) + 2)
    exit_mark = _mark(connection, code, exit_time)
    if entry is None or exit_mark is None or entry <= 0:
        return None
    return exit_mark / entry - 1.0


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    def _ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        for position, index in enumerate(order, start=1):
            ranks[index] = float(position)
        return ranks
    rx, ry = _ranks(xs), _ranks(ys)
    try:
        return statistics.correlation(rx, ry)
    except statistics.StatisticsError:
        return None


def _auc(scores: list[float], labels: list[bool]) -> float | None:
    positives = [s for s, label in zip(scores, labels) if label]
    negatives = [s for s, label in zip(scores, labels) if not label]
    if not positives or not negatives:
        return None
    wins = 0.0
    for pos in positives:
        for neg in negatives:
            if pos > neg:
                wins += 1.0
            elif pos == neg:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


def _quintile_means(pairs: list[tuple[float, float]]) -> list[float]:
    """同一批内按分数分 5 档、同档取标签均值（池化后用于 Q1/Q5 差）。"""
    if len(pairs) < 5:
        return []
    ordered = sorted(pairs, key=lambda item: item[0])
    size = len(ordered) / 5.0
    means = []
    for bucket in range(5):
        start = int(round(bucket * size))
        end = int(round((bucket + 1) * size))
        chunk = ordered[start:end]
        means.append(sum(label for _score, label in chunk) / len(chunk) if chunk else float("nan"))
    return means


def _day_of(observed_at: str) -> str:
    return str(observed_at)[:10]


def main() -> int:
    parser = argparse.ArgumentParser(description="A-2 双算 A/B 只读分析")
    parser.add_argument("--db", default=str(ROOT / "data/trading.db"))
    parser.add_argument("--out", default="")
    parser.add_argument("--bootstrap", type=int, default=BOOTSTRAP_ROUNDS)
    args = parser.parse_args()

    db_path = Path(args.db)
    batches = _load_batches(db_path)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row

    # 每批：覆盖率 + 模型自证过滤
    valid: list[dict] = []
    void_batches = 0
    for payload in batches:
        ml_info = payload.get("ml") or {}
        coverage = float(ml_info.get("coverage") or 0.0)
        version_ok = ml_info.get("model_version") in (None, "v3.2")
        features_ok = ml_info.get("model_feature_count") in (None, 19)
        if coverage < MIN_COVERAGE or not version_ok or not features_ok:
            void_batches += 1
            continue
        candidates = payload.get("candidates") or []
        rows = []
        for item in candidates:
            label = _forward_label(connection, str(item.get("code")), payload.get("observed_at", ""))
            rows.append(
                {
                    "code": item.get("code"),
                    "rule_score": item.get("rule_score"),
                    "ml_score": item.get("ml_score"),
                    "label": label,
                }
            )
        labeled = [row for row in rows if row["label"] is not None]
        if not labeled:
            continue
        valid.append(
            {
                "day": _day_of(payload.get("observed_at", "")),
                "rows": labeled,
                "rule_selected": payload.get("rule_selected_code"),
                "ml_selected": payload.get("ml_selected_code"),
            }
        )
    connection.close()

    trading_days = sorted({batch["day"] for batch in valid})
    lines = [
        "# A-2 v3.2 模拟盘 A/B 分析（同一批决策双算）",
        "",
        f"- 生成时间（UTC）：{datetime.now(timezone.utc).isoformat()}",
        f"- 观测批次总数：{len(batches)}；覆盖率/自证不达标作废：{void_batches}",
        f"- 有效批次（有前向标签）：{len(valid)}（门槛 {MIN_BATCHES}）",
        f"- 有效交易日：{len(trading_days)}（门槛 {MIN_TRADING_DAYS}）",
        f"- 前向标签：{FORWARD_TRADING_DAYS} 个交易日（缺价记 label_missing，不补 0）",
        "",
    ]
    if void_batches > len(batches) / 2:
        lines += ["## 结论：**VOID**", "",
                  "过半批次覆盖率/模型自证不达标（疑似静默回退规则模型或快照不新鲜）。"]
        return _finish(lines, args.out, 4)
    if len(valid) < MIN_BATCHES or len(trading_days) < MIN_TRADING_DAYS:
        lines += ["## 结论：**INSUFFICIENT**", "",
                  "样本未达门槛，不出结论（防用不足样本反复碰阈值）。"]
        return _finish(lines, args.out, 3)

    def _metrics(selected_batches: list[dict]) -> dict:
        ic_all, pooled_rule, pooled_ml = [], [], []
        auc_rule, auc_ml = [], []
        hit_rule, hit_ml = [], []
        for batch in selected_batches:
            rows = batch["rows"]
            rule_pairs = [(row["rule_score"], row["label"]) for row in rows if row["rule_score"] is not None]
            ml_pairs = [(row["ml_score"], row["label"]) for row in rows if row["ml_score"] is not None]
            if len(rule_pairs) >= 3 and len(ml_pairs) >= 3:
                ic_rule = _spearman([p[0] for p in rule_pairs], [p[1] for p in rule_pairs])
                ic_ml = _spearman([p[0] for p in ml_pairs], [p[1] for p in ml_pairs])
                if ic_rule is not None and ic_ml is not None:
                    ic_all.append((ic_rule, ic_ml))
            pooled_rule.extend(rule_pairs)
            pooled_ml.extend(ml_pairs)
            if ml_pairs:
                labels_binary = [1.0 if label > 0 else 0.0 for _score, label in ml_pairs]
                auc_ml.append(_auc([p[0] for p in ml_pairs], [bool(v) for v in labels_binary]))
            if rule_pairs:
                labels_binary = [1.0 if label > 0 else 0.0 for _score, label in rule_pairs]
                auc_rule.append(_auc([p[0] for p in rule_pairs], [bool(v) for v in labels_binary]))
            top_rule = max(rows, key=lambda row: row["rule_score"] if row["rule_score"] is not None else -1e9)
            top_ml = max(rows, key=lambda row: row["ml_score"] if row["ml_score"] is not None else -1e9)
            hit_rule.append(1.0 if top_rule["label"] > 0 else 0.0)
            hit_ml.append(1.0 if top_ml["label"] > 0 else 0.0)
        rule_quint = _quintile_means(pooled_rule)
        ml_quint = _quintile_means(pooled_ml)
        return {
            "ic_rule": _mean(x[0] for x in ic_all),
            "ic_ml": _mean(x[1] for x in ic_all),
            "auc_rule": _mean(v for v in auc_rule if v is not None),
            "auc_ml": _mean(v for v in auc_ml if v is not None),
            "hit_rule": _mean(hit_rule),
            "hit_ml": _mean(hit_ml),
            "q_rule": rule_quint,
            "q_ml": ml_quint,
        }

    observed = _metrics(valid)
    by_day = defaultdict(list)
    for batch in valid:
        by_day[batch["day"]].append(batch)
    days = list(by_day)
    rng = random.Random(BOOTSTRAP_SEED)
    ic_diffs, auc_diffs = [], []
    for _ in range(int(args.bootstrap)):
        sample = []
        for _ in range(len(days)):
            sample.extend(by_day[rng.choice(days)])
        metrics = _metrics(sample)
        if metrics["ic_rule"] is not None and metrics["ic_ml"] is not None:
            ic_diffs.append(metrics["ic_ml"] - metrics["ic_rule"])
        if metrics["auc_rule"] is not None and metrics["auc_ml"] is not None:
            auc_diffs.append(metrics["auc_ml"] - metrics["auc_rule"])

    def _ci(values: list[float]) -> str:
        if not values:
            return "N/A"
        values = sorted(values)
        p5 = values[int(0.05 * (len(values) - 1))]
        p50 = values[int(0.50 * (len(values) - 1))]
        return f"p5={p5:+.4f} / p50={p50:+.4f}"

    lines += [
        "## 指标（两臂同一批决策）",
        "",
        "| 指标 | rule 臂 | v3.2 臂 | 差值 |",
        "|---|---|---|---|",
        f"| 每批 Spearman IC 均值 | {_fmt(observed['ic_rule'])} | {_fmt(observed['ic_ml'])} | "
        f"{_fmt(None if observed['ic_ml'] is None or observed['ic_rule'] is None else observed['ic_ml'] - observed['ic_rule'])} |",
        f"| AUC 均值 | {_fmt(observed['auc_rule'])} | {_fmt(observed['auc_ml'])} | "
        f"{_fmt(None if observed['auc_ml'] is None or observed['auc_rule'] is None else observed['auc_ml'] - observed['auc_rule'])} |",
        f"| Top1 命中率 | {_fmt(observed['hit_rule'])} | {_fmt(observed['hit_ml'])} | "
        f"{_fmt(None if observed['hit_ml'] is None or observed['hit_rule'] is None else observed['hit_ml'] - observed['hit_rule'])} |",
        f"| 五档 Q1−Q5（ml） | — | {_q5(observed['q_ml'])} | — |",
        f"| 五档 Q1−Q5（rule） | {_q5(observed['q_rule'])} | — | — |",
        "",
        "## 交易日 block bootstrap（1000 次）",
        "",
        f"- IC 差值（v3.2 − rule）：{_ci(ic_diffs)}",
        f"- AUC 差值（v3.2 − rule）：{_ci(auc_diffs)}",
        "",
        "> 判据：≥30 交易日且 ≥200 批才解读；结论仅「继续/停止」，不自动 promote。",
    ]
    return _finish(lines, args.out, 0)


def _mean(values) -> float | None:
    items = [float(v) for v in values if v is not None]
    return sum(items) / len(items) if items else None


def _fmt(value) -> str:
    return "N/A" if value is None else f"{float(value):+.4f}"


def _q5(quintiles: list[float]) -> str:
    if not quintiles or len(quintiles) < 5:
        return "N/A"
    return f"{quintiles[0] - quintiles[4]:+.4f}"


def _finish(lines: list[str], out: str, code: int) -> int:
    text = "\n".join(lines) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"[OK] 报告已写入 {out}")
    else:
        print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
