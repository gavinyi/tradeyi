"""G3 执行成本归因报表守卫（`scripts/exec_cost_attribution.py`，只读）。

覆盖点（每一项都在真库上踩过坑）：
1. **两个费用来源必须同时报**：`order_intents.fees`（意图级）与 `fill_ledger.fee_total`
   （成交级）实测差约 $1000，只取其一会把成本说小一半；
2. **滑点方向**：BUY 成交价高于参考价=不利（正成本），SELL 反之；美元成本按
   `diff × quantity × multiplier`；
3. **失败必须分桶**（限额/急停/对账/监管/持仓资金/其他），否则 82 REJECTED 仍是一锅粥；
4. **「有值但恒为 0」的成本列必须点名**（spread_cost/price_impact_cost/commission），
   否则报告会让人误以为在统计；
5. **只读**：报表前后数据库文件字节不变；
6. **GBK 控制台安全**：脚本含 ⚠️ 输出，必须走兜底 print（今日第 4 次同类事故的延伸）。
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for extra in (str(ROOT), str(SCRIPTS)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import exec_cost_attribution as cost_report  # noqa: E402

SCHEMA = (
    """
    CREATE TABLE order_intents (
        id INTEGER PRIMARY KEY, strategy_id TEXT, symbol TEXT, side TEXT, status TEXT,
        quantity REAL, filled_quantity REAL, average_fill_price REAL, fees REAL,
        metadata_json TEXT, created_at TEXT, updated_at TEXT, reprice_count INTEGER,
        error TEXT
    )
    """,
    """
    CREATE TABLE fill_ledger (
        id INTEGER PRIMARY KEY, intent_id INTEGER, strategy_id TEXT, symbol TEXT, side TEXT,
        quantity REAL, price REAL, multiplier REAL, commission REAL, regulatory_fee REAL,
        fee_total REAL, bid_price REAL, ask_price REAL, mid_price REAL, reference_price REAL,
        spread_cost REAL, price_impact_cost REAL, source TEXT, filled_at TEXT
    )
    """,
)


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "trading.db"
    conn = sqlite3.connect(db)
    for statement in SCHEMA:
        conn.execute(statement)
    conn.commit()
    conn.close()
    return db


def _insert_intent(
    db: Path, *, intent_id: int, strategy="volatility_long_gamma", status="FILLED",
    side="BUY", quantity=1.0, filled=1.0, avg_price=1.0, fees=0.0,
    reference=None, error=None, created="2026-08-05T14:00:00+00:00",
    updated="2026-08-05T14:01:00+00:00", reprice=0,
) -> None:
    metadata = '{"multiplier": 100.0}'
    if reference is not None:
        metadata = f'{{"multiplier": 100.0, "reference_price": {reference}}}'
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO order_intents (id, strategy_id, symbol, side, status, quantity, "
        "filled_quantity, average_fill_price, fees, metadata_json, created_at, updated_at, "
        "reprice_count, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (intent_id, strategy, "US.SPY260807C500000", side, status, quantity, filled,
         avg_price, fees, metadata, created, updated, reprice, error),
    )
    conn.commit()
    conn.close()


def _insert_fill(
    db: Path, *, strategy="volatility_long_gamma", side="BUY", quantity=1.0, price=1.0,
    multiplier=100.0, fee_total=0.0, commission=0.0, reference=1.0,
    spread=0.0, impact=0.0, source="LEGACY_INTENT_AGGREGATE",
    filled_at="2026-08-05T14:00:30+00:00",
) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO fill_ledger (strategy_id, symbol, side, quantity, price, multiplier, "
        "commission, regulatory_fee, fee_total, bid_price, ask_price, mid_price, "
        "reference_price, spread_cost, price_impact_cost, source, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (strategy, "US.SPY260807C500000", side, quantity, price, multiplier, commission,
         0.0, fee_total, None, None, None, reference, spread, impact, source, filled_at),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ 费用
def test_cost_summary_and_fee_bps(tmp_path):
    """成本必须取 `order_intents.fees`，账本占位值只能作对比基线。"""
    db = _make_db(tmp_path)
    _insert_intent(db, intent_id=1, fees=2.25)
    _insert_intent(db, intent_id=2, side="SELL", fees=2.25)
    _insert_fill(db, price=2.0, quantity=1.0, multiplier=100.0, fee_total=1.0)
    _insert_fill(db, price=2.0, quantity=1.0, multiplier=100.0, fee_total=1.0,
                 side="SELL", filled_at="2026-08-05T15:00:00+00:00")
    conn = cost_report._connect(db)
    try:
        result = cost_report.cost_summary(conn)
    finally:
        conn.close()
    assert result["fills"] == 2
    assert result["notional"] == pytest.approx(400.0)
    # 权威口径 = intents 全费用估算，不是账本 $1 占位值之和
    assert result["fees"] == pytest.approx(4.5)
    assert result["fees_filled_only"] == pytest.approx(4.5)
    assert result["fee_bps"] == pytest.approx(112.5)
    assert result["fees_are_estimate"] is True
    assert result["ledger_fees"] == pytest.approx(2.0)
    assert result["ledger_fee_bps"] == pytest.approx(50.0)


def test_cost_summary_excludes_unfilled_intents_from_filled_estimate(tmp_path):
    db = _make_db(tmp_path)
    _insert_intent(db, intent_id=1, fees=2.5)
    _insert_intent(db, intent_id=2, status="REJECTED", fees=9.0)
    _insert_fill(db, price=2.0, quantity=1.0, multiplier=100.0, fee_total=1.0)
    conn = cost_report._connect(db)
    try:
        result = cost_report.cost_summary(conn)
    finally:
        conn.close()
    assert result["fees"] == pytest.approx(11.5)
    assert result["fees_filled_only"] == pytest.approx(2.5)


def test_fee_reconciliation_surfaces_two_source_delta(tmp_path):
    db = _make_db(tmp_path)
    _insert_intent(db, intent_id=1, fees=100.0)
    _insert_fill(db, fee_total=40.0)
    conn = cost_report._connect(db)
    try:
        result = cost_report.fee_reconciliation(conn)
    finally:
        conn.close()
    assert result["intent_fees_total"] == pytest.approx(100.0)
    assert result["ledger_fee_total"] == pytest.approx(40.0)
    assert result["delta"] == pytest.approx(60.0)
    assert result["consistent"] is False
    assert "不得只取其一" in result["note"]


def test_fee_reconciliation_reports_consistency_when_equal(tmp_path):
    db = _make_db(tmp_path)
    _insert_intent(db, intent_id=1, fees=12.5)
    _insert_fill(db, fee_total=12.5)
    conn = cost_report._connect(db)
    try:
        result = cost_report.fee_reconciliation(conn)
    finally:
        conn.close()
    assert result["delta"] == pytest.approx(0.0)
    assert result["consistent"] is True


def test_fee_reconciliation_flags_placeholder_1_dollar(tmp_path):
    """实测根因守卫：fill_ledger 大量 fee_total 恒为 1.0（占位值）。

    这种情况下**成交级总额不可当成本**，权威口径必须切到 intents.fees（estimate 全费用）。
    """
    db = _make_db(tmp_path)
    for index in range(10):
        _insert_intent(db, intent_id=index + 1, fees=2.5075)
        _insert_fill(db, fee_total=1.0, filled_at=f"2026-08-0{index % 5 + 1}T14:0{index}:00+00:00")
    conn = cost_report._connect(db)
    try:
        result = cost_report.fee_reconciliation(conn)
    finally:
        conn.close()
    assert result["ledger_modal_fee_value"] == pytest.approx(1.0)
    assert result["ledger_modal_fee_rows"] == 10
    assert result["ledger_reliable"] is False
    assert "estimate" in result["authoritative"]
    assert "不可当成本引用" in result["note"]


# ------------------------------------------------------------------ 滑点
def test_slippage_sign_and_bps_across_buy_sell(tmp_path):
    db = _make_db(tmp_path)
    # BUY：成交 1.10 vs 参考 1.00 → 不利 0.10 × 100 = +$10
    _insert_fill(db, side="BUY", price=1.10, reference=1.00, quantity=1.0, multiplier=100.0,
                 strategy="volatility_long_gamma")
    # SELL：成交 0.90 vs 参考 1.00 → 不利 0.10 × 100 = +$10
    _insert_fill(db, side="SELL", price=0.90, reference=1.00, quantity=1.0, multiplier=100.0,
                 strategy="core_satellite", filled_at="2026-08-05T15:00:00+00:00")
    conn = cost_report._connect(db)
    try:
        result = cost_report.slippage_summary(conn)
    finally:
        conn.close()
    assert result["samples"] == 2
    assert result["total_usd"] == pytest.approx(20.0)
    assert result["worse_than_ref"] == 2
    assert result["worse_ratio"] == pytest.approx(1.0)
    assert result["bps"]["mean_bps"] == pytest.approx(1000.0)
    assert result["by_strategy"]["volatility_long_gamma"]["total_usd"] == pytest.approx(10.0)
    assert result["by_strategy"]["core_satellite"]["total_usd"] == pytest.approx(10.0)


def test_slippage_ignores_rows_without_reference(tmp_path):
    db = _make_db(tmp_path)
    _insert_fill(db, price=1.0, reference=None)
    conn = cost_report._connect(db)
    try:
        result = cost_report.slippage_summary(conn)
    finally:
        conn.close()
    assert result["samples"] == 0
    assert result["total_usd"] == 0.0


# ------------------------------------------------------------------ 失败归因
@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("实盘账户当日累计名义金额将超过 $5,000.00", "限额"),
        ("系统急停已开启，禁止新增仓位", "急停"),
        ("启动恢复发现三账差异，请先完成持仓归属与对账", "对账"),
        ("根据中国内地监管要求，您当前位于中国内地，不可交易", "监管"),
        ("place simulated order failed: 持仓不足", "持仓资金"),
        ("完全未知的错误", "其他"),
        ("", "无原因文本"),
    ],
)
def test_classify_failure_buckets(reason, expected):
    assert cost_report.classify_failure(reason) == expected


def test_failure_summary_groups_into_buckets_with_shares(tmp_path):
    db = _make_db(tmp_path)
    _insert_intent(db, intent_id=1, status="REJECTED", error="当日累计名义金额将超过 $5,000")
    _insert_intent(db, intent_id=2, status="REJECTED", error="系统急停已开启，禁止新增仓位")
    _insert_intent(db, intent_id=3, status="FAILED", error="place simulated order failed: 持仓不足")
    _insert_intent(db, intent_id=4, status="FILLED", filled=1.0)
    conn = cost_report._connect(db)
    try:
        result = cost_report.failure_summary(conn)
    finally:
        conn.close()
    assert result["by_bucket"]["限额"] == 1
    assert result["by_bucket"]["急停"] == 1
    assert result["by_bucket"]["持仓资金"] == 1
    total_share = sum(result["share_by_bucket"].values())
    assert total_share == pytest.approx(1.0)


# ------------------------------------------------------------ 成本列健康度
def test_dead_cost_columns_are_flagged(tmp_path):
    db = _make_db(tmp_path)
    _insert_fill(db, spread=0.0, impact=0.0, commission=0.0)
    conn = cost_report._connect(db)
    try:
        problems = cost_report.dead_column_check(conn)
    finally:
        conn.close()
    joined = " ".join(problems)
    assert "spread_cost" in joined and "price_impact_cost" in joined and "commission" in joined


# -------------------------------------------------------------- 只读 + 报告
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_render_writes_markdown_and_is_read_only(tmp_path):
    db = _make_db(tmp_path)
    _insert_intent(db, intent_id=1, fees=5.0)
    # 账本 $1 占位：报表的每交易日费用必须按权威估算($5.0)算，不能取占位值
    _insert_fill(db, price=2.0, reference=1.9, fee_total=1.0)
    before = _sha256(db)
    report, summary = cost_report.render(db)
    assert _sha256(db) == before, "报表修改了数据库（必须只读）"
    for section in ("成交成本", "两个费用来源对账", "滑点：UNKNOWN", "失败原因分桶",
                    "每日费用（权威估算"):
        assert section in report
    # 冻结口径的三条文案必须留在报告里，防被悄悄改回「以账本为准」
    assert "全费用估算，非已扣款" in report
    assert "不可引用" in report
    assert summary["slippage_status"] == "UNKNOWN"
    assert summary["fee_reconciliation"]["delta"] == pytest.approx(4.0)
    assert summary["fee_per_day"] == pytest.approx(5.0)
    assert summary["cost"]["ledger_fees"] == pytest.approx(1.0)


def test_render_marks_missing_db(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(sqlite3.OperationalError):
        cost_report.render(missing)


# -------------------------------------------------------------- GBK 安全
def test_main_survives_gbk_stdout(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _insert_intent(db, intent_id=1, fees=1.0)
    _insert_fill(db, fee_total=1.0, spread=0.0)
    raw = io.BytesIO()
    gbk_stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", gbk_stream)
    monkeypatch.setattr(sys, "argv", ["exec_cost_attribution.py", "--db", str(db)])
    assert cost_report.main() == 0
    gbk_stream.flush()
    assert raw.getvalue(), "GBK 流上没有输出"
