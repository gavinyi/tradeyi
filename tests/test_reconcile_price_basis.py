"""成交维度口径拆分守卫（2026-11-07 Product 收紧裁定）。

B1 决策对照（无需价格，今晚出数）/ no_price_basis 三纪律（不进分母不计通过、
与 L1/L2/L3 同屏、数据可得性守卫必红）/ backtest_bug 分档互斥。
"""

from __future__ import annotations

import json

from futu_quant.signal_backtest import ledger as bt_ledger
from futu_quant.signal_backtest.ledger import (
    ReconcileRow, decision_agreement, reconcile,
)


class _Trade:
    def __init__(self, created_at, symbol, fill_price):
        self.created_at = created_at
        self.symbol = symbol
        self.fill_price = fill_price


def _fill(created_at, symbol, price):
    return {"filled_at": created_at, "symbol": symbol, "price": price}


def _evt(created_at, symbol, tenant="t1"):
    class E:
        pass
    event = E()
    event.created_at = created_at
    event.symbol = symbol
    event.tenant_id = tenant
    event.payload = {"zscore_20d": 2.5, "intraday_return": 0.05}  # 自洽 payload（对齐 replay 直读级行形态）
    return event


# ---------- no_price_basis 分档 ----------

def test_no_simulated_fill_not_backtest_bug():
    """裁定核心：无价格依据窗口的实际成交落独立档，绝不进 backtest_bug。"""
    report = reconcile([], [_fill("2026-11-05T14:00:00", "US.AMD", 2.5)],
                       simulated_fill_available=False)
    dist = report.reason_distribution()
    assert dist["no_simulated_fill"] == 1
    assert dist["backtest_bug"] == 0
    assert not report.has_backtest_bug


def test_mutually_exclusive_buckets():
    """分档互斥：同一行只能落一个档。"""
    report = reconcile([], [
        _fill("2026-11-05T14:00:00", "US.A", 2.5),
        _fill("2026-11-05T15:00:00", "US.B", 1.5),
    ], simulated_fill_available=False)
    for row in report.rows:
        reasons = [r for r in bt_ledger.ALL_REASONS if r == row.reason]
        assert len(reasons) == 1
        if row.reason == "no_simulated_fill":
            assert row.reason != "backtest_bug"


def test_no_price_basis_excluded_from_ratio_denominator():
    """纪律①：不进分母、不计通过——全 no_price_basis 时 price_reconcile_ratio=None。"""
    report = reconcile([], [_fill("2026-11-05T14:00:00", "US.A", 2.5)],
                       simulated_fill_available=False)
    assert report.price_reconcile_ratio is None, "必须返回 None（写「不适用」），禁「100%」"


def test_price_ratio_only_over_eligible_rows():
    """有可对照行时比率只按 eligible 行算（no_price_basis 行不稀释）。

    注：分档由 simulated_fill_available 整窗决定（互斥即此意），故「混合」
    形态通过两次调用对比验证——available=False 全落独立档且比率为 None。
    """
    report_ok = reconcile(
        [_Trade("2026-11-05T14:00:00", "US.A", 2.5)],
        [_fill("2026-11-05T14:00:00", "US.A", 2.5)],
        simulated_fill_available=True,
    )
    assert report_ok.no_price_basis_count == 0
    assert report_ok.price_reconcile_ratio == 1.0
    report_np = reconcile(
        [], [_fill("2026-11-05T15:00:00", "US.B", 1.5)],
        simulated_fill_available=False,
    )
    assert report_np.no_price_basis_count == 1
    assert report_np.price_reconcile_ratio is None


# ---------- 数据可得性守卫（纪律③：有价可撮却落 no_price_basis → 红） ----------

def test_availability_guard_contradiction_is_explicit():
    """CLI 可得性判定必须是组合态（replayed×fills），绑定对照内容两侧。"""
    from pathlib import Path
    from futu_quant.signal_backtest import ledger as lb

    src = (Path(__file__).resolve().parents[1] / "scripts" / "backtest_cli.py"
           ).read_text(encoding="utf-8")
    assert "bool(result.replayed_events) and bool(fills)" in src, \
        "CLI 可得性判定必须组合态绑定（replayed 与 fills 两侧对照内容）"
    assert lb.REASON_NO_SIMULATED_FILL in lb.ALL_REASONS


def test_match_error_cancels_exemption():
    """回归守卫（Tester 2026-11-07 收官全量实证）：撮合层故障不得被豁免吞掉。

    最终形态（Reviewer 组合态判据）：豁免唯一条件 = replayed 空或 fills 空
    （真空数据期）；「replayed 非空 + 实际成交存在 + 零撮合产出」无论异常
    类型一律 available=True → backtest_bug（PRD 硬门槛 5）。AST 钉死
    available 判据必须同时看 replayed 与 fills（组合态），不许退回单看
    bool(trades)（那正是被变异绕过的旧形态）。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "backtest_cli.py"
           ).read_text(encoding="utf-8")
    assert "simulated_fill_available = (" in src
    assert "bool(result.replayed_events) and bool(fills)" in src, \
        "豁免判据必须是组合态（replayed×fills），禁退回 bool(trades) 单条件"
    assert "except ValueError:" in src
    assert "match_errors += 1" in src
    # 异常计数分支先于 available 判定执行
    assert src.index("match_errors += 1") < src.index("simulated_fill_available =")


# ---------- B1 决策结论对照 ----------

def test_b1_decision_agreement_counts_both_directions():
    """B1：重放放行 vs 实际下单意图同向率——无需价格数据。"""
    replayed = [_evt("2026-11-05T14:03:00", "US.AMD"),
                _evt("2026-11-05T15:03:00", "US.QQQ")]
    intents = [{"created_at": "2026-11-05T14:03:30", "symbol": "US.AMD"}]
    result = decision_agreement(replayed, intents)
    assert result["both_count"] == 1
    assert result["agreement_ratio"] == 0.5
    assert result["replay_only_count"] == 1
    assert result["replay_only"][0]["symbol"] == "US.QQQ"
    assert "candidate_filtered" in result["replay_only"][0]["tentative_reason"], \
        "replay_only 必须带初步归因（「为什么没成交」第一层答案）"


def test_b1_empty_intents_zero_agreement():
    result = decision_agreement([_evt("2026-11-05T14:03:00", "US.AMD")], [])
    assert result["agreement_ratio"] == 0.0 and result["both_count"] == 0


def test_b1_three_line_verdict_shape():
    """Product 三行判据：actual_only / replay_only 原因分布 / 未归因 0。"""
    # replay_only（自洽 payload）→ 初步归因 candidate_filtered；actual_only 计数
    result = decision_agreement(
        [_evt("2026-11-05T14:03:00", "US.AMD")],
        [{"created_at": "2026-11-05T16:00:00", "symbol": "US.OTHER"}],
    )
    assert result["actual_only_count"] == 1, "实际下单而回放未放行必须独立计数"
    assert result["replay_only_count"] == 1
    assert result["replay_only"][0]["tentative_reason"].startswith("candidate_filtered")
    assert result["unattributed_count"] == 0, "自洽 payload 的差异必须可初归因"


def test_b1_bare_payload_is_unattributed():
    """payload 无触发依据 → unattributed（未归因必须为 0 的判据才有意义）。"""
    class Bare:
        created_at = "2026-11-05T14:03:00"
        symbol = "US.BARE"
        tenant_id = "t1"
        payload: dict = {}

    result = decision_agreement([Bare()], [])
    assert result["replay_only_count"] == 1
    assert result["unattributed_count"] == 1
    assert result["replay_only"][0]["tentative_reason"].startswith("unattributed")


# ---------- 变异红证 ----------

def test_mutation_folding_no_price_basis_into_backtest_bug_must_be_red():
    """变异：把 no_price_basis 行折回 backtest_bug → 互斥守卫必红（防「免罪牌」回退）。"""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    target = (Path(__file__).resolve().parents[1]
              / "src" / "futu_quant" / "signal_backtest" / "ledger.py")
    original = target.read_text(encoding="utf-8")
    anchor = '                reason=REASON_NO_SIMULATED_FILL,'
    assert anchor in original, "分档锚点丢失"
    mutated = original.replace(anchor, '                reason=REASON_BACKTEST_BUG,', 1)
    assert mutated != original
    try:
        target.write_text(mutated, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"{Path(__file__)}::test_no_simulated_fill_not_backtest_bug",
             "--capture=no"],
            capture_output=True, text=True, cwd=str(target.parents[2]),
        )
        assert result.returncode != 0, "分档被折回后守卫仍绿——backtest_bug 可被数据缺口污染"
    finally:
        target.write_text(original, encoding="utf-8")
        shutil.rmtree(target.parents[2] / ".pytest_cache", ignore_errors=True)
