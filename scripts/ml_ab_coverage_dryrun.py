#!/usr/bin/env python3
"""A-2 切键前**只读**覆盖率自证脚本（2026-10-27）。

为什么必须先跑这个：`_apply_ml_ranking` 在 `scoring_mode=rule` 时**直接 return**
（`volatility_roll.py:428`），所以切键前运行路径根本不会算 ML 分 —— 「覆盖率 ≥80%」无法
在切键后用运行数据补救（那时 paper 已经按 v3.2 真下单了）。本脚本用**当前真实候选**做一次
离线打分，给出覆盖率证据；**不写任何配置、不下单、不改库**。

判据（与 A-2 方案 §0-⑤ 一致）：
- 覆盖率 = 有真实 `ml_score` 的候选 / 参与打分的候选，**必须 ≥ 80%**；
- 快照时效必须先过：最新 `option_history` 快照距现在 ≤45 分钟（= `SNAPSHOT_MAX_LAG_MINUTES`），
  否则判 `OUTSIDE_RTH_OR_STALE_SNAPSHOT`（收盘后跑必然全 MISS，结论无意义）。

用法（**盘中运行**）：
    python scripts/ml_ab_coverage_dryrun.py --tenant futu_paper_sandbox \
        --out data/qa/ml_ab_coverage_dryrun_<date>.md
退出码：0=覆盖率达标；1=覆盖率不达标；2=快照不新鲜/数据不足（不可判定）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from futu_quant.ml_scorer import (  # noqa: E402
    SNAPSHOT_MAX_LAG_MINUTES,
    MissingFeatureError,
    latest_underlying_snapshot,
    predict_symbol_score,
)
from futu_quant.ml_symbol_scorer import MLSymbolScorer  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402

DEFAULT_MODEL_NAME = "volatility_scorer_candidate_20261025_091240.pkl"
COVERAGE_THRESHOLD = 0.80


def _candidate_codes(db_path: Path, tenant: str, lookback_days: int) -> list[str]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "select payload_json from decision_logs where strategy_id='volatility_long_gamma' "
            "and tenant_id=? and payload_json like '%top_candidates%' "
            "and created_at >= datetime('now', ?) order by id desc limit 400",
            (tenant, f"-{int(lookback_days)} days"),
        ).fetchall()
    finally:
        connection.close()
    codes: list[str] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        for item in (payload.get("diagnostics") or {}).get("top_candidates") or []:
            code = str(item.get("code") or "").strip()
            if code and code not in codes:
                codes.append(code)
    return codes


def _snapshot_freshness(db_path: Path) -> tuple[str | None, float | None]:
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute("select max(time_key) from option_history").fetchone()
    finally:
        connection.close()
    raw = str(row[0]) if row and row[0] else None
    if not raw:
        return None, None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return raw, None
    if stamp.tzinfo is None:  # time_key 口径：naive = ET
        from zoneinfo import ZoneInfo

        stamp = stamp.replace(tzinfo=ZoneInfo("America/New_York"))
    return raw, (datetime.now(timezone.utc) - stamp).total_seconds() / 60.0


def main() -> int:
    parser = argparse.ArgumentParser(description="A-2 切键前覆盖率只读自证")
    parser.add_argument("--tenant", default="futu_paper_sandbox")
    parser.add_argument(
        "--model-path", default="",
        help=f"默认 <db 目录>/ml_models/{DEFAULT_MODEL_NAME}",
    )
    parser.add_argument("--lookback-days", type=int, default=5)
    parser.add_argument("--max-codes", type=int, default=40)
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--db", default="",
        help="trading.db 路径（默认按当前工作目录解析；从 worktree 只读核对生产库时可显式指定）",
    )
    args = parser.parse_args()

    store = TradingStore(args.db) if args.db else TradingStore()
    db_path = Path(args.db) if args.db else Path(getattr(store, "db_path", ROOT / "data/trading.db"))
    model_path = Path(args.model_path) if args.model_path else (
        db_path.parent / "ml_models" / DEFAULT_MODEL_NAME
    )
    latest_snapshot, lag_minutes = _snapshot_freshness(db_path)
    lines = [
        "# A-2 切键前覆盖率自证（只读）",
        "",
        f"- 运行时间（UTC）：{datetime.now(timezone.utc).isoformat()}",
        f"- 租户：`{args.tenant}`",
        f"- 模型：`{model_path}`",
        f"- 最新 option_history 快照：`{latest_snapshot}`，滞后 "
        f"{(round(lag_minutes, 1) if lag_minutes is not None else 'N/A')} 分钟"
        f"（阈值 {SNAPSHOT_MAX_LAG_MINUTES} 分钟）",
        "",
    ]
    if lag_minutes is None or lag_minutes > SNAPSHOT_MAX_LAG_MINUTES:
        lines += [
            "## 结论：**OUTSIDE_RTH_OR_STALE_SNAPSHOT（不可判定）**",
            "",
            "快照不新鲜时 `moneyness`/`gamma_to_theta` 必然缺失，全部候选都会 MISS —— "
            "这种覆盖率数字没有意义。请在**美股盘中**（快照 ≤45 分钟）重跑本脚本。",
        ]
        text = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        print(text)
        return 2

    codes = _candidate_codes(db_path, args.tenant, args.lookback_days)[: args.max_codes]
    if not codes:
        lines += ["## 结论：**INSUFFICIENT_DATA（无候选）**", "", "近期没有可复用的候选快照。"]
        text = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        print(text)
        return 2

    if not model_path.exists():
        print(f"[FAIL] 模型文件不存在：{model_path}（用 --model-path 指定）")
        return 2
    scorer = MLSymbolScorer(model_path=str(model_path), threshold=0.5, enabled=True)
    now = datetime.now(timezone.utc)
    scored = 0
    rows: list[tuple[str, str]] = []
    missing_by_code: dict[str, str] = {}
    for code in codes:
        reason = ""
        try:
            score = predict_symbol_score(
                code, scorer.model, scorer.feature_cols, store, now,
            )
            if score is None:
                reason = "ml_score_unavailable"
            else:
                scored += 1
        except MissingFeatureError as exc:
            reason = str(exc).strip()[:160]
        except Exception as exc:  # noqa: BLE001 - 逐候选容错，单个失败不影响整体统计
            reason = f"{type(exc).__name__}: {str(exc)[:120]}"
        if reason:
            missing_by_code[code] = reason
        snapshot = latest_underlying_snapshot(store, code, now)
        rows.append(
            (
                code,
                "OK" if not reason else reason,
                round(float(snapshot.get("lag_minutes") or -1), 1)
                if snapshot.get("spot") is not None else None,
            )
        )

    coverage = scored / len(codes)
    verdict = "PASS" if coverage >= COVERAGE_THRESHOLD else "FAIL"
    lines += [
        f"- 候选数：{len(codes)}（来自近 {args.lookback_days} 天 `volatility_long_gamma` 决策快照）",
        f"- 成功打分：{scored}",
        f"- **覆盖率：{coverage:.1%}**（门槛 {COVERAGE_THRESHOLD:.0%}）",
        f"- 模型 version={scorer.model is not None}，feature_count={len(scorer.feature_cols)}",
        "",
        f"## 结论：**{verdict}**",
        "",
        "| code | 结果 | 快照滞后(分钟) |",
        "|---|---|---|",
    ]
    lines += [f"| `{code}` | {status} | {lag if lag is not None else '—'} |" for code, status, lag in rows]
    if missing_by_code:
        lines += ["", "### 缺失原因分布", ""]
        counts: dict[str, int] = {}
        for reason in missing_by_code.values():
            counts[reason] = counts.get(reason, 0) + 1
        lines += [f"- {count} 次：{reason}" for reason, count in sorted(
            counts.items(), key=lambda item: -item[1]
        )]
    lines += [
        "",
        "> 本脚本只读：不写配置、不改库、不下单。覆盖率 ≥80% 才允许切 "
        "`scoring_mode:futu_paper_sandbox=ml`。",
    ]
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[OK] 证据已写入 {args.out}")
    print(f"coverage={coverage:.1%} verdict={verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
