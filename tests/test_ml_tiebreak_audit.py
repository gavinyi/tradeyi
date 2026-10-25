"""收盘后 tie-break 落地审计（`scripts/ml_tiebreak_audit.py`）验收守卫。

背景：兜底 rule tie-break 的 realised 分位必须**长期可见**，但数据源原来只有页面按钮。
本单加每日收盘后独立审计（Chief 2026-10-25 裁定，黄区），五条验收：
1. 幂等键 = 交易日 + 口径版本，同一天重复运行**保留首条 + 跳过**（`skipped_duplicate` 计数）；
2. 无并列组 / 样本不足 → 落带 `reason` 的审计 + ERROR 运行事件，**不抛异常**；
3. 走 `run_task` 并设超时，**不加载模型、不跑 `run_ml_vs_rule_comparison`**（不许阻塞主循环）；
4. payload 与页面点击那条**同结构**（同键名同分段），否则合并统计会分裂；
5. 失败必须落 ERROR 事件，不许只有 print。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ml_tiebreak_audit as audit  # noqa: E402
from futu_quant.ml_scorer import (  # noqa: E402
    rule_tiebreak_realised_quality,
    summarise_tiebreak_history,
)
from futu_quant.store import TradingStore  # noqa: E402

CSV_PATH = ROOT / "data/ml_training/volatility_trades_v3_latest.csv"


def _store(tmp_path: Path) -> TradingStore:
    return TradingStore(tmp_path / "audit.db")


def _payloads(store: TradingStore) -> list[dict]:
    result = []
    for row in store.audit_events(200) or []:
        if row.get("event_type") != audit.AUDIT_EVENT:
            continue
        result.append(json.loads(row.get("payload_json") or "{}"))
    return result


def _error_events(store: TradingStore) -> list[dict]:
    return [
        row for row in store.operational_events(200) or []
        if row.get("event_type") == "ML_TIEBREAK_AUDIT" and row.get("status") == "ERROR"
    ]


def _require_csv() -> pd.DataFrame:
    if not CSV_PATH.exists():
        pytest.skip(f"缺少回测 CSV {CSV_PATH}")
    frame = pd.read_csv(CSV_PATH)
    frame["trade_date"] = frame["trade_date"].astype(str)
    return frame.dropna(subset=["trade_date"]).reset_index(drop=True)


def _tie_less_csv(tmp_path: Path) -> Path:
    """可控 CSV：各行旧离散分互不相同 → 无并列组（应落 reason，不该抛）。"""
    rows = [
        {
            "trade_date": "2026-08-05",
            "window_net_pnl": pnl,
            "symbol_win_rate": rate,
            "symbol_pnl_trend": trend,
            "symbol_avg_pnl": 5.0 if rate > 0.5 else -5.0,
            "underlying_symbol": "US.SPY",
            "strike_price": 400.0 + index,
            "is_call": 1,
            "days_to_expiry": 3 + index,
            "expiry_bucket": 1,
        }
        for index, (pnl, rate, trend) in enumerate([
            (10.0, 0.30, -1.0),
            (20.0, 0.40, -1.0),
            (30.0, 0.60, 1.0),
            (40.0, 0.70, 1.0),
            (50.0, 0.80, 2.0),
        ])
    ]
    path = tmp_path / "tie_less.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    return path


# ------------------------------------------------------------------ 主路径
def test_audit_writes_one_row_with_page_compatible_payload(tmp_path):
    """payload 与页面点击那条同结构：融合/单盐双分位 + 5 段 + 交易日 + 口径版本。"""
    frame = _require_csv()
    store = _store(tmp_path)
    result = audit.run_ml_tiebreak_audit(store, CSV_PATH)

    assert result["status"] == "ok", result
    payloads = _payloads(store)
    assert len(payloads) == 1
    payload = payloads[0]

    # 与 rule_tiebreak_realised_quality 同键名（页面合并统计依赖这一点）
    reference = rule_tiebreak_realised_quality(frame)
    for key in ("fused_percentile", "single_salt_percentile", "segments",
                "groups", "pairs", "fusion_k", "status"):
        assert key in payload, f"payload 缺少页面统计依赖的键 {key}"
        assert key in reference
    assert payload["status"] == "ok"
    assert payload["trade_date"] == str(frame["trade_date"].max())
    assert payload["metric_version"] == audit.METRIC_VERSION
    assert payload["source"] == "scheduled_post_close"
    assert len(payload["segments"]) == 5

    rows = [
        row for row in store.audit_events(50) or []
        if row.get("event_type") == audit.AUDIT_EVENT
    ]
    assert rows[0]["actor"] == "service:ml_tiebreak_audit"
    assert _error_events(store) == []


def test_audit_is_idempotent_per_trade_date(tmp_path):
    """同一天重复运行：保留首条、跳过并累加计数，滚动统计只看到 1 条。"""
    _require_csv()
    store = _store(tmp_path)
    first = audit.run_ml_tiebreak_audit(store, CSV_PATH)
    assert first["status"] == "ok"

    second = audit.run_ml_tiebreak_audit(store, CSV_PATH)
    assert second["status"] == "skipped_duplicate"
    assert second["skipped_duplicate"] == 1
    assert len(_payloads(store)) == 1, "同日重复运行写了重复行 → 会拉偏滚动均值"
    assert store.get_setting(audit.SKIPPED_DUPLICATE_KEY, "0") == "1"

    summary = summarise_tiebreak_history(_payloads(store))
    assert summary["status"] == "ok"
    assert summary["samples"] == 1


# --------------------------------------------------------------- 失败可见性
def test_audit_records_reason_without_raising_when_no_tie_groups(tmp_path):
    """无并列组 / 样本不足：落 reason + ERROR 事件，不抛异常、不静默。"""
    store = _store(tmp_path)
    result = audit.run_ml_tiebreak_audit(store, _tie_less_csv(tmp_path))

    assert result["status"] == "unavailable"
    assert result["reason"] in {"insufficient_sample", "missing_columns"}
    payloads = _payloads(store)
    assert len(payloads) == 1
    assert payloads[0]["status"] == "unavailable"
    assert payloads[0]["reason"]
    assert _error_events(store), "数据不可用必须落 ERROR 运行事件，不许只有 print"


def test_audit_missing_csv_records_reason_and_raises(tmp_path):
    """CSV 缺失属硬失败：写 reason 审计 + ERROR 事件，再抛给调度标失败。"""
    store = _store(tmp_path)
    missing = tmp_path / "no_such_回测.csv"
    with pytest.raises(RuntimeError):
        audit.run_ml_tiebreak_audit(store, missing)

    payloads = _payloads(store)
    assert len(payloads) == 1
    assert payloads[0]["status"] == "unavailable"
    assert payloads[0]["reason"] == "csv_unreadable"
    assert _error_events(store)


def test_audit_dry_run_writes_nothing(tmp_path):
    _require_csv()
    store = _store(tmp_path)
    result = audit.run_ml_tiebreak_audit(store, CSV_PATH, dry_run=True)
    assert result["status"] == "ok"
    assert _payloads(store) == []
    assert _error_events(store) == []


# ------------------------------------------------------- 接线与依赖边界守卫
def _code_names(source: str) -> set[str]:
    """只取**代码标识符**（名字/属性/导入），文档字符串与注释里的文字不算。"""
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_run_service_wires_audit_without_comparator():
    """接线守卫：走 run_task + 120s 超时；**代码里**不得出现对照回测/模型加载。"""
    source = (ROOT / "scripts" / "run_service.py").read_text(encoding="utf-8")
    assert "from ml_tiebreak_audit import run_ml_tiebreak_audit" in source
    assert '"ml_tiebreak_audit": 120' in source
    collapsed = " ".join(source.split())
    assert (
        'run_task( store, alerts, "ml_tiebreak_audit", '
        'lambda: run_ml_tiebreak_audit(store), )'
    ) in collapsed, "审计任务必须走 run_task（带超时/心跳/告警），不能直接裸调"
    names = _code_names(source)
    assert "run_ml_vs_rule_comparison" not in names, (
        "run_service 不许调用对照回测（要读 CSV + 打分，会阻塞主循环）"
    )
    assert "load_ml_model" not in names, "调度侧不许加载 ML 模型"


def test_audit_script_does_not_load_model_or_run_comparison():
    """脚本边界守卫：只算 rule 分位，不加载模型、不跑对照回测。"""
    source = (ROOT / "scripts" / "ml_tiebreak_audit.py").read_text(encoding="utf-8")
    assert "rule_tiebreak_realised_quality" in source
    names = _code_names(source)
    assert "load_ml_model" not in names
    assert "run_ml_vs_rule_comparison" not in names
    assert audit.AUDIT_EVENT == "RULE_TIEBREAK_REALISED_QUALITY", (
        "事件名必须与页面点击那条一致，否则合并统计会分裂成两套"
    )


def test_audit_settings_keys_are_explicitly_global():
    """Reviewer 口径：审计是全局口径（同一份 CSV），状态键必须显式 `global:` 前缀。

    不许误用 P0-8 租户键 helper（`_sched_write_key`）——那会让两个 Worker 各写各的租户键、
    计数互相不可见；源码里也要写明这是全局口径而非租户级。
    """
    assert audit.SKIPPED_DUPLICATE_KEY.startswith("global:")
    assert audit.LAST_ACK_KEY.startswith("global:")
    source = (ROOT / "scripts" / "ml_tiebreak_audit.py").read_text(encoding="utf-8")
    assert "全局调度状态键" in source
    assert "不是租户级" in source


def test_sampling_rate_follows_csv_export_frequency():
    """Reviewer 口径：采样率 = 训练 CSV 导出频率，不是每日——脚本与页面都必须写明。

    否则 Ops 会按「每天 1 条」核查，长期空窗会被误判成脚本没生效。
    """
    script = (ROOT / "scripts" / "ml_tiebreak_audit.py").read_text(encoding="utf-8")
    assert "采样率" in script
    assert "训练 CSV 导出频率" in script
    assert "不假设日更" in script or "不能按「每天 1 条」核查" in script
    page = (ROOT / "pages" / "42_ml_model_status.py").read_text(encoding="utf-8")
    assert "采样率与训练 CSV 导出频率一致" in page
    assert "skipped_duplicate" in page
