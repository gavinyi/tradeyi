"""Ops 巡检新增两项的守卫（P1-07 CSV 新鲜度 + 快照断采）。

变异可证：
- 把 `check_snapshot_gaps` 的成交源从 `order_intents` 换回 `fill_ledger` → 断言红
  （这正是写这条检查时踩到的坑：fill_ledger 只有 30 个交易日，会静默漏掉 8 月那批）；
- 把 `ML_CSV_REQUIRED_COLUMNS` 删一列 → 缺列检测红。
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta, timezone
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
    # Chief 2026-10-25 定稿：这里用「常量集合全等」做自检，防的是有人**删掉必填列**；
    # 运行期只判「缺列」（`missing = [c for c in REQUIRED if c not in header]`），
    # CSV 多列不会导致失败 —— 加特征列是常态，不为此设白名单摩擦。
    assert set(audit.ML_CSV_REQUIRED_COLUMNS) == {
        "moneyness",
        "gamma_to_theta",
        "moneyness_missing_reason",
        "gamma_to_theta_missing_reason",
        "moneyness_lag_minutes",
    }


def test_csv_extra_columns_do_not_alert(tmp_path):
    """多列不是错误：加特征列时巡检必须保持静默（Chief 已撤回白名单方案）。"""
    audit = _import_audit()
    path = tmp_path / "data" / "ml_training" / "volatility_trades_v3_latest.csv"
    today = datetime.now(timezone.utc).date().isoformat()
    row = {
        "trade_date": today, "moneyness": "0.01", "gamma_to_theta": "1.2",
        "moneyness_missing_reason": "OK", "gamma_to_theta_missing_reason": "OK",
        "moneyness_lag_minutes": "0.7", "brand_new_feature": "1",
    }
    _write_csv(path, ["trade_date", "brand_new_feature", *REQUIRED], [row])

    assert audit.check_csv_freshness(tmp_path) == [], "多列不得产生告警"


def test_csv_staleness_fail_closed_when_date_unparsable():
    """P1-09：陈旧度算不出来时必须告警（fail-closed），不得 return [] 静默。"""
    audit = _import_audit()
    issues = audit._csv_staleness_issue("not-a-date", 5)
    assert issues, "日期解析失败必须告警，不许静默当作『不陈旧』"
    assert "fail-closed" in issues[0]
    assert "无法判定" in issues[0]


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


# ------------------------------------------------------- P1-14 费用占位值


def _seed_fees(store, fees: list[float], days_ago: int = 1) -> None:
    """往 fill_ledger 写 N 行，`fee_total` 依次取给定值，`filled_at` 设为 N 天前。"""
    filled_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with store.connection() as connection:
        for idx, fee in enumerate(fees):
            connection.execute(
                "INSERT INTO fill_ledger"
                " (intent_id,strategy_id,symbol,side,quantity,price,fee_total,source,filled_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (idx + 1, "volatility_long_gamma", "US.SPY", "BUY", 1, 1.0,
                 fee, "futu_push", filled_at),
            )


def test_fee_placeholder_ratio_alerts_when_high(tmp_path):
    audit = _import_audit()
    store = TradingStore(tmp_path / "fees.db")
    _seed_fees(store, [1.0] * 9 + [2.4] * 1)  # 窗口内 90% 占位

    issues = audit.check_fee_placeholder_ratio(store)
    assert len(issues) == 1
    assert "占位值占比" in issues[0]
    assert "9/10" in issues[0]
    assert "order_intents.fees" in issues[0], "必须指向权威口径，避免被当成成本真值"


def test_fee_placeholder_ratio_silent_when_low(tmp_path):
    audit = _import_audit()
    store = TradingStore(tmp_path / "fees.db")
    _seed_fees(store, [2.4] * 20 + [1.0] * 1)  # 窗口内约 4.8%，低于 5% 阈值

    assert audit.check_fee_placeholder_ratio(store) == []


def test_fee_placeholder_ratio_silent_when_empty(tmp_path):
    audit = _import_audit()
    store = TradingStore(tmp_path / "fees.db")
    assert audit.check_fee_placeholder_ratio(store) == []


def test_fee_placeholder_ratio_ignores_legacy_rows(tmp_path):
    """历史 689 行占位不回填：不能因为它们按全表占比 90% 就天天告警。"""
    audit = _import_audit()
    store = TradingStore(tmp_path / "fees.db")
    _seed_fees(store, [1.0] * 50, days_ago=60)  # 全是 60 天前的历史行
    _seed_fees(store, [2.4] * 10, days_ago=1)   # 最近的都已是估算值

    assert audit.check_fee_placeholder_ratio(store) == [], "窗口外历史占位不得触发告警"


def test_fee_ledger_summary_line_shows_both_scopes(tmp_path):
    """全表占比仍必须可见（信息项），只是不触发告警。

    合并 fill-fee 分支后新建库已带三态列，文案走「历史占位 + 未标注 1.0 哨兵」两段；
    这里只断言关键数字都出现，不锁死具体措辞。
    """
    audit = _import_audit()
    store = TradingStore(tmp_path / "fees.db")
    _seed_fees(store, [1.0] * 5, days_ago=60)
    _seed_fees(store, [2.4] * 5, days_ago=1)

    line = audit.fee_ledger_summary_line(store)
    assert "费用账本状态" in line
    assert "5/10" in line, "全表 5 行 1.0 必须显示"
    assert "最近 7 天" in line and "0/5" in line


def _add_fee_state_columns(store) -> None:
    """模拟迁移 17/18：给 fill_ledger 补三态两列（真实迁移落地前测试用）。

    合并 fill-fee 分支后，新建库本身已带这两列 —— 先探测再补，否则 `ALTER TABLE`
    会报 `duplicate column name`（两支各自绿、合起来红，2026-10-25 全量实测到）。
    """
    with store.connection() as connection:
        existing = {row[1] for row in connection.execute("pragma table_info(fill_ledger)")}
        if "fee_is_estimate" not in existing:
            connection.execute(
                "ALTER TABLE fill_ledger ADD COLUMN fee_is_estimate INTEGER NOT NULL DEFAULT 0"
            )
        if "fee_unavailable_reason" not in existing:
            connection.execute(
                "ALTER TABLE fill_ledger ADD COLUMN fee_unavailable_reason TEXT"
            )


def _set_fee_state(store, row_id: int, *, estimate: int = 0, reason: str | None = None) -> None:
    with store.connection() as connection:
        connection.execute(
            "UPDATE fill_ledger SET fee_is_estimate=?, fee_unavailable_reason=?"
            " WHERE id=?",
            (estimate, reason, row_id),
        )


def test_fee_placeholder_judge_ignores_estimated_one_dollar(tmp_path):
    """三态列就位后：估算值恰好 1.00（fee_is_estimate=1）不得算作占位误报。"""
    audit = _import_audit()
    store = TradingStore(tmp_path / "fees.db")
    _add_fee_state_columns(store)
    _seed_fees(store, [1.0] * 10, days_ago=1)  # 10 行 1.0，需逐行标注
    with store.connection() as connection:
        ids = [r[0] for r in connection.execute("SELECT id FROM fill_ledger").fetchall()]
    for row_id in ids:
        _set_fee_state(store, row_id, estimate=1)  # 全部是「估算值恰好 1.00」

    assert audit.check_fee_placeholder_ratio(store) == [], "估算态的 1.0 不是占位值"


def test_fee_placeholder_judge_ignores_marked_legacy_rows(tmp_path):
    """迁移 19 已把历史行标 legacy_placeholder_1usd：不算进新告警。"""
    audit = _import_audit()
    store = TradingStore(tmp_path / "fees.db")
    _add_fee_state_columns(store)
    _seed_fees(store, [1.0] * 10, days_ago=1)
    with store.connection() as connection:
        ids = [r[0] for r in connection.execute("SELECT id FROM fill_ledger").fetchall()]
    for row_id in ids:
        _set_fee_state(store, row_id, reason="legacy_placeholder_1usd")

    assert audit.check_fee_placeholder_ratio(store) == []


def test_fee_placeholder_judge_alerts_on_unlabelled_one_dollar(tmp_path):
    """「未标注的 1.0」才是真占位：三态列就位后仍必须告警。"""
    audit = _import_audit()
    store = TradingStore(tmp_path / "fees.db")
    _add_fee_state_columns(store)
    _seed_fees(store, [1.0] * 10, days_ago=1)  # 全部未标注

    issues = audit.check_fee_placeholder_ratio(store)
    assert issues and "占位值占比" in issues[0]


def test_fee_summary_counts_null_filled_at_separately(tmp_path):
    audit = _import_audit()
    store = TradingStore(tmp_path / "fees.db")
    _add_fee_state_columns(store)
    _seed_fees(store, [1.0, 2.4], days_ago=1)
    # 表定义是 `filled_at TEXT NOT NULL`，实际只能是空串（NULL 写不进去）
    with store.connection() as connection:
        connection.execute("UPDATE fill_ledger SET filled_at='' WHERE id=1")

    line = audit.fee_ledger_summary_line(store)
    assert "filled_at` 为空" in line
    assert "1 行" in line


def test_fee_summary_keeps_legacy_visible_after_migration(tmp_path):
    """迁移 19 之后：历史 689 行仍必须显式可见，且与「未标注 1.0」分开显示。

    否则新判据会把它们过滤掉，全表显示成 0，看起来像「历史已经修好」。
    """
    audit = _import_audit()
    store = TradingStore(tmp_path / "fees.db")
    _add_fee_state_columns(store)
    _seed_fees(store, [1.0] * 10, days_ago=1)
    with store.connection() as connection:
        ids = [r[0] for r in connection.execute("SELECT id FROM fill_ledger").fetchall()]
    for row_id in ids:
        _set_fee_state(store, row_id, reason="legacy_placeholder_1usd")

    line = audit.fee_ledger_summary_line(store)
    assert "legacy_placeholder_1usd" in line, "历史占位标记必须可见"
    assert "10/10" in line, "历史占位计数不能被新判据过滤成 0"
    assert "未标注 1.0" in line and "0/10" in line, "回归哨兵单独显示"
