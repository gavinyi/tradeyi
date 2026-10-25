"""Ops 巡检新增两项的守卫（P1-07 CSV 新鲜度 + 快照断采）。

变异可证：
- 把 `check_snapshot_gaps` 的成交源从 `order_intents` 换回 `fill_ledger` → 断言红
  （这正是写这条检查时踩到的坑：fill_ledger 只有 30 个交易日，会静默漏掉 8 月那批）；
- 把 `ML_CSV_REQUIRED_COLUMNS` 删一列 → 缺列检测红。
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from futu_quant.store import TradingStore  # noqa: E402


def _import_audit():
    return pytest.importorskip("scripts.scheduled_log_audit")


# ---------------------------------------------------------------- P1-07 CSV


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


REQUIRED = [
    "moneyness",
    "gamma_to_theta",
    "moneyness_missing_reason",
    "gamma_to_theta_missing_reason",
    "moneyness_lag_minutes",
]


def test_csv_missing_reports_actionable_issue(tmp_path):
    audit = _import_audit()
    issues = audit.check_csv_freshness(tmp_path)
    assert issues, "CSV 缺失必须报警，不许静默"
    assert "不存在" in issues[0]
    assert "ml_export_training_data_v3.py" in issues[0], "必须给出重导命令，否则没法处置"


def test_csv_missing_columns_reports_old_export(tmp_path):
    audit = _import_audit()
    path = tmp_path / "data" / "ml_training" / "volatility_trades_v3_latest.csv"
    cols = ["trade_date", "moneyness"]  # 故意缺其余必需列
    _write_csv(path, cols, [{"trade_date": "2026-10-22", "moneyness": "0.01"}])

    issues = audit.check_csv_freshness(tmp_path)
    assert issues and "缺列" in issues[0], "旧版导出（无真行情列）必须报出来"
    for col in ("gamma_to_theta", "moneyness_lag_minutes"):
        assert col in issues[0]


def test_csv_low_coverage_reports(tmp_path):
    audit = _import_audit()
    path = tmp_path / "data" / "ml_training" / "volatility_trades_v3_latest.csv"
    rows = [
        {"trade_date": "2026-10-22", "moneyness": "0.01", "gamma_to_theta": "1.2",
         "moneyness_missing_reason": "OK", "gamma_to_theta_missing_reason": "OK",
         "moneyness_lag_minutes": "0.7"},
        {"trade_date": "2026-10-22", "moneyness": "", "gamma_to_theta": "",
         "moneyness_missing_reason": "snapshot_missing_or_stale",
         "gamma_to_theta_missing_reason": "missing", "moneyness_lag_minutes": ""},
    ]
    _write_csv(path, ["trade_date", *REQUIRED], rows)

    issues = audit.check_csv_freshness(tmp_path)
    assert any("覆盖率不足" in i for i in issues)


def test_csv_healthy_silent(tmp_path):
    audit = _import_audit()
    path = tmp_path / "data" / "ml_training" / "volatility_trades_v3_latest.csv"
    today = datetime.now(timezone.utc).date().isoformat()
    rows = [
        {"trade_date": today, "moneyness": "0.01", "gamma_to_theta": "1.2",
         "moneyness_missing_reason": "OK", "gamma_to_theta_missing_reason": "OK",
         "moneyness_lag_minutes": "0.7"}
    ]
    _write_csv(path, ["trade_date", *REQUIRED], rows)

    assert audit.check_csv_freshness(tmp_path) == [], "健康数据不得产生假告警"


def test_csv_required_columns_constant_covers_true_market_columns():
    audit = _import_audit()
    for col in ("moneyness", "gamma_to_theta", "moneyness_lag_minutes"):
        assert col in audit.ML_CSV_REQUIRED_COLUMNS


# ------------------------------------------------------- 快照断采


def _seed(store, *, fills: list[tuple[str, str]], snapshots: list[str]) -> None:
    """fills: [(created_at, status)]；snapshots: [time_key]"""
    with store.connection() as connection:
        for created_at, status in fills:
            connection.execute(
                "INSERT INTO order_intents"
                " (strategy_id,intent_key,symbol,side,quantity,limit_price,status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                ("volatility_long_gamma", f"k-{created_at}-{status}", "US.SPY",
                 "BUY", 1, 1.0, status, created_at, created_at),
            )
        for time_key in snapshots:
            connection.execute(
                "INSERT INTO option_history"
                " (code,time_key,underlying,underlying_price,bid,ask,iv,delta,gamma,theta,volume,open_interest)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("US.SPY261120C700000", time_key, "US.SPY", 700.0, 1.0, 1.1, 0.2,
                 0.5, 0.01, -0.2, 10, 100),
            )


def test_snapshot_gap_detected_for_filled_day(tmp_path):
    audit = _import_audit()
    store = TradingStore(tmp_path / "t.db")
    _seed(
        store,
        fills=[("2026-08-05T14:00:00+00:00", "FILLED"),
               ("2026-08-11T14:00:00+00:00", "FILLED")],
        snapshots=["2026-08-11 10:00:00"],
    )

    issues = audit.check_snapshot_gaps(store)
    assert len(issues) == 1
    assert "2026-08-05" in issues[0]
    assert "1 笔" in issues[0]


def test_snapshot_gap_ignores_non_filled(tmp_path):
    audit = _import_audit()
    store = TradingStore(tmp_path / "t.db")
    _seed(store, fills=[("2026-08-05T14:00:00+00:00", "SHADOW_FILLED")], snapshots=[])

    assert audit.check_snapshot_gaps(store) == [], "非 FILLED 不该误报"


def test_snapshot_gap_no_gap_silent(tmp_path):
    audit = _import_audit()
    store = TradingStore(tmp_path / "t.db")
    _seed(
        store,
        fills=[("2026-08-11T14:00:00+00:00", "FILLED")],
        snapshots=["2026-08-11 10:00:00"],
    )

    assert audit.check_snapshot_gaps(store) == []
