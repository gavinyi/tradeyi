"""收盘后 tie-break 落地质量审计（每日一条，**不加载模型、不跑对照回测**）。

背景：兜底 rule 打分器的 tie-break 在模型加载失败时接管选品，但它的 realised 分位是
「一个排序的一次落地值」，必须长期可见而不是点一次页面看一次。本脚本收盘后单独算一次
并写 `audit_events` 一条（payload 与页面点击那条**同结构**，便于合并「最近 N 条」统计）。

约束（Chief 2026-10-25 裁定，黄区）：
- **只读 CSV + 复用 `rule_tiebreak_realised_quality`**，绝不加载 ML 模型、绝不调用
  `run_ml_vs_rule_comparison`（那条要读 CSV + 打分，塞进调度会阻塞主循环）；
- 幂等键 = `(CSV 最大交易日, 口径版本)`：同一天重复运行**保留首条 + 跳过**，只累加
  `skipped_duplicate` 计数，不写重复行（否则滚动均值会被一天的多次运行拉偏）；
- **采样率 = 训练 CSV 导出频率，不是每日**：CSV 不重导时，每天运行只会 `skipped_duplicate`；
  监控统计按「最近 N 条」采样，禁止按「每天 1 条」核查（否则长期空窗会被误判成故障）；
- 失败必须可见：CSV 缺失 / 样本不足 / 无并列组都写一条带 `reason` 的审计 + ERROR 运行事件，
  不静默 return；CSV 读不了这类硬失败额外抛错，让调度把任务标成失败并告警。

用法：
    python scripts/ml_tiebreak_audit.py
    python scripts/ml_tiebreak_audit.py --csv data/ml_training/volatility_trades_v3_latest.csv
    python scripts/ml_tiebreak_audit.py --db data/trading.db --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.ml_scorer import (  # noqa: E402
    RULE_SCORER_VERSION,
    rule_tiebreak_realised_quality,
)
from futu_quant.store import TradingStore  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_CSV = ROOT / "data/ml_training/volatility_trades_v3_latest.csv"
DEFAULT_DB = ROOT / "data/trading.db"
AUDIT_EVENT = "RULE_TIEBREAK_REALISED_QUALITY"
AUDIT_ACTOR = "service:ml_tiebreak_audit"
# 口径版本进幂等键：口径一变（哈希/融合/度量定义），同日可以再写一条新的，不覆盖旧的。
METRIC_VERSION = f"tiebreak-fused-k8-v1|{RULE_SCORER_VERSION}"
# 以下两个是**全局调度状态键**：审计口径本来就是全局的（同一份训练 CSV、同一套口径版本），
# 不是租户级。显式 `global:` 前缀 + 本注释，避免误用 P0-8 的租户键 helper
# （`_sched_write_key` 会写租户键 → 两个 Worker 各写各的、计数互相不可见）。
# 采样率 = **训练 CSV 导出频率**，不是每日：幂等键取 CSV 最大交易日，CSV 不重导时
# 每日运行只会得到 `skipped_duplicate`，不会产生新行（滚动统计因此按条数，不假设日更）。
SKIPPED_DUPLICATE_KEY = "global:ml_tiebreak_audit:skipped_duplicate"
LAST_ACK_KEY = "global:ml_tiebreak_audit:last_ack"


def _load_frame(csv_path: Path) -> pd.DataFrame:
    """读回测 CSV 并规整 trade_date；读不了时抛 FileNotFoundError/ValueError（硬失败）。"""
    if not csv_path.exists():
        raise FileNotFoundError(f"tie-break 审计缺少回测 CSV：{csv_path}")
    frame = pd.read_csv(csv_path)
    if frame.empty:
        raise ValueError(f"tie-break 审计的回测 CSV 为空：{csv_path}")
    if "trade_date" not in frame.columns:
        if "created_at" not in frame.columns:
            raise ValueError(
                "回测 CSV 既无 trade_date 也无 created_at，无法按交易日审计："
                f"{csv_path}"
            )
        frame["trade_date"] = frame["created_at"].astype(str).str[:10]
    frame["trade_date"] = frame["trade_date"].astype(str)
    return frame.dropna(subset=["trade_date"])


def _existing_scopes(store: TradingStore) -> set[tuple[str, str]]:
    """已入库的 `(交易日, 口径版本)` 组合（用于同日幂等）。"""
    scopes: set[tuple[str, str]] = set()
    for row in store.audit_events(1000) or []:
        if row.get("event_type") != AUDIT_EVENT:
            continue
        if row.get("actor") != AUDIT_ACTOR:
            continue
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, ValueError):
            continue
        trade_date = payload.get("trade_date")
        metric_version = payload.get("metric_version")
        if trade_date and metric_version:
            scopes.add((str(trade_date), str(metric_version)))
    return scopes


def _write_skipped_counter(store: TradingStore, previous: str) -> int:
    try:
        count = int(previous or "0") + 1
    except (TypeError, ValueError):
        count = 1
    store.set_setting(SKIPPED_DUPLICATE_KEY, str(count))
    return count


def _record(store: TradingStore, payload: dict) -> None:
    """落一条审计 + 不可用/失败时落 ERROR 运行事件（不静默）。"""
    store.audit(AUDIT_EVENT, payload, actor=AUDIT_ACTOR)
    if payload.get("status") == "ok":
        return
    store.operational_event(
        "ml",
        "ML_TIEBREAK_AUDIT",
        "ERROR",
        {
            "reason": payload.get("reason"),
            "trade_date": payload.get("trade_date"),
            "metric_version": payload.get("metric_version"),
            "missing": payload.get("missing"),
            "groups": payload.get("groups"),
            "pairs": payload.get("pairs"),
        },
    )


def run_ml_tiebreak_audit(
    store: TradingStore,
    csv_path: Path | str | None = None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """执行一次收盘后审计；返回本次结果 dict（幂等跳过返回 `status="skipped_duplicate"`）。"""
    path = Path(csv_path) if csv_path else DEFAULT_CSV
    checked_at = (now or datetime.now()).isoformat(timespec="seconds")
    base: dict = {
        "metric_version": METRIC_VERSION,
        "csv_path": str(path),
        "checked_at": checked_at,
    }

    try:
        frame = _load_frame(path)
    except (FileNotFoundError, ValueError) as exc:
        # 硬失败：写一条带 reason 的审计 + ERROR 事件，再抛给调度（任务标失败、触发告警）
        payload = {
            **base,
            "status": "unavailable",
            "reason": "csv_unreadable",
            "error": f"{type(exc).__name__}: {exc}",
        }
        if not dry_run:
            _record(store, payload)
        logger.error("tie-break 审计失败（CSV 不可用）：%s", exc)
        raise RuntimeError(f"tie-break 审计无法读取回测 CSV：{exc}") from exc

    trade_date = str(frame["trade_date"].max())
    payload_base = {**base, "trade_date": trade_date}

    if not dry_run and (trade_date, METRIC_VERSION) in _existing_scopes(store):
        skipped = _write_skipped_counter(
            store, store.get_setting(SKIPPED_DUPLICATE_KEY, "0")
        )
        logger.info(
            "tie-break 审计幂等跳过：交易日 %s（口径 %s）已存在，保留首条（累计跳过 %s 次）",
            trade_date, METRIC_VERSION, skipped,
        )
        return {
            **payload_base,
            "status": "skipped_duplicate",
            "skipped_duplicate": skipped,
        }

    quality = rule_tiebreak_realised_quality(frame)
    payload = {**payload_base, **quality, "source": "scheduled_post_close"}
    if not dry_run:
        _record(store, payload)
        if payload.get("status") == "ok":
            store.set_setting(LAST_ACK_KEY, f"{trade_date}|{METRIC_VERSION}|{checked_at}")
        else:
            logger.error(
                "tie-break 审计数据不可用（交易日 %s）：reason=%s missing=%s groups=%s pairs=%s",
                trade_date,
                payload.get("reason"),
                payload.get("missing"),
                payload.get("groups"),
                payload.get("pairs"),
            )
    else:
        logger.info("tie-break 审计 dry-run：未写库，结果 %s", payload.get("status"))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="收盘后 tie-break 落地质量审计（不加载模型）")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="回测 CSV 路径")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    parser.add_argument("--dry-run", action="store_true", help="只算不写库")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    store = TradingStore(args.db)
    try:
        result = run_ml_tiebreak_audit(store, args.csv, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("tie-break 审计执行失败：%s", exc)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
