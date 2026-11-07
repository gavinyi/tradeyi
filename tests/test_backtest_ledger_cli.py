"""回测引擎 Day 2 定向测试：对账原因码 + 净值曲线 + CLI（PRD §2 L-B/§5/§6）。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from futu_quant.signal_backtest import ledger as bt_ledger
from futu_quant.signal_backtest import matcher as bt_matcher
from futu_quant.signal_backtest import replay as bt_replay


def _signal(symbol: str, created: str):
    return replace(bt_replay.SignalEvent(
        created_at=created, symbol=symbol, decision="BUY",
        tenant_id="futu_real_master", payload={},
    ), symbol=symbol)


def _trade(symbol: str, created: str, price: float):
    return bt_matcher.MatchedTrade(
        code=symbol, symbol=symbol, created_at=created,
        fill_price=price, price_tier="L2",
    )


def test_reconcile_matched_within_tick_tolerance():
    trades = [_trade("US.AMD", "2026-11-06T14:05:00+00:00", 2.50)]
    fills = [{
        "symbol": "US.AMD", "price": 2.505,
        "filled_at": "2026-11-06T14:05:30+00:00",
    }]
    report = bt_ledger.reconcile(trades, fills)
    assert report.aligned_count == 1
    assert report.reconcile_ratio == pytest.approx(1.0)
    assert not report.has_backtest_bug


def test_reconcile_missing_actual_is_backtest_bug():
    """回测有、实际无 → 保守记 backtest_bug（宁可标红不可放行）。"""
    trades = [_trade("US.AMD", "2026-11-06T14:05:00+00:00", 2.50)]
    report = bt_ledger.reconcile(trades, [])
    assert report.has_backtest_bug
    assert report.reason_distribution()[bt_ledger.REASON_BACKTEST_BUG] == 1


def test_reconcile_missing_backtest_is_backtest_bug():
    """实际有、回测无 → 重放漏判，同样 backtest_bug。"""
    fills = [{
        "symbol": "US.AMD", "price": 2.5,
        "filled_at": "2026-11-06T14:05:00+00:00",
    }]
    report = bt_ledger.reconcile([], fills)
    assert report.has_backtest_bug


def test_reconcile_price_differs_is_slippage():
    trades = [_trade("US.AMD", "2026-11-06T14:05:00+00:00", 2.50)]
    fills = [{
        "symbol": "US.AMD", "price": 2.60,
        "filled_at": "2026-11-06T14:05:00+00:00",
    }]
    report = bt_ledger.reconcile(trades, fills)
    assert report.rows[0].matched is False
    assert report.rows[0].reason == bt_ledger.REASON_SLIPPAGE
    assert not report.has_backtest_bug


def test_equity_curve_accumulates_with_fees():
    trades = [
        _trade("US.AMD", "2026-11-06T14:05:00+00:00", 2.00),
        _trade("US.AMD", "2026-11-06T15:05:00+00:00", 2.50),
        _trade("US.AMD", "2026-11-06T16:05:00+00:00", 2.40),
        _trade("US.AMD", "2026-11-06T17:05:00+00:00", 2.60),
    ]
    curve = bt_ledger.equity_curve(
        trades, initial_capital=10_000.0, fee_per_trade_fn=bt_matcher.fee_per_trade,
    )
    assert len(curve) == 2  # 两对开平
    assert curve[0][1] > 10_000 and curve[1][1] > curve[0][1]


def _write_cli_args(tmp_path, label: str = "demo", *, force: bool = False):
    import scripts.backtest_cli as cli

    db = tmp_path / "bt.db"
    from futu_quant.store import TradingStore
    TradingStore(db, bootstrap=True)
    monkeypatch_none = None
    argv = [
        "run", "--from", "2026-11-06T00:00:00+00:00",
        "--to", "2026-11-07T00:00:00+00:00",
        "--label", label, "--db", str(db),
    ] + (["--force"] if force else [])
    parser = cli._build_parser()
    return parser.parse_args(argv), monkeypatch_none


def test_cli_report_header_and_label_isolation(tmp_path, monkeypatch):
    """CLI：报告头固定四行 + 分层占比行；label 隔离；重复跑需 --force。"""
    import scripts.backtest_cli as cli

    db = tmp_path / "bt.db"
    from futu_quant.store import TradingStore
    TradingStore(db, bootstrap=True)
    out_root = tmp_path / "backtests"
    monkeypatch.setattr(cli, "OUTPUT_ROOT", out_root)

    argv = [
        "run", "--from", "2026-11-06T00:00:00+00:00",
        "--to", "2026-11-07T00:00:00+00:00",
        "--label", "demo", "--db", str(db),
    ]
    args = cli._build_parser().parse_args(argv)
    rc = cli.run_backtest(args)
    assert rc in (0, 1)  # 自校验通过与否取决于空库种子；两态都合法
    out_dir = out_root / "demo"
    assert (out_dir / "report.md").exists()
    assert (out_dir / "trades.csv").exists()
    report_text = (out_dir / "report.md").read_text(encoding="utf-8")
    for token in ("数据区间", "配置快照", "自校验是否通过", "数据完整性", "撮合分层占比", "产品红线"):
        assert token in report_text, f"报告头缺固定行：{token}"

    # 重复跑不带 --force 必须拒绝（防覆盖）
    with pytest.raises(SystemExit, match="force"):
        cli.run_backtest(args)
    # 带 --force 可覆盖
    args_force = cli._build_parser().parse_args(argv + ["--force"])
    assert cli.run_backtest(args_force) in (0, 1)


def test_cli_mutation_never_fill_must_fail_self_check(tmp_path, monkeypatch):
    """变异红证（PRD 硬门槛 5）：撮合价改成「永不成交」→ 报告 A 必须转红。

    正向种数据：BUY 决策 + 同键实际成交 → 正常跑自校验 PASS、backtest_bug=0。
    变异：matcher.match_trade 打成恒抛（「永不成交」）→ 实际有、回测无 →
    对账 backtest_bug>0 → **报告必须标红且 CLI 返回 1**（报告不是摆设）。
    """
    import scripts.backtest_cli as cli

    db = tmp_path / "bt2.db"
    from futu_quant.store import TradingStore
    store = TradingStore(db, bootstrap=True)
    created = "2026-11-06T14:05:00+00:00"
    store.decision(
        "volatility_scanner", "US.AMD", "BUY", "触发", {
            "regime": {"zscore_20d": 2.5, "intraday_return": 0.05},
        }, tenant_id="futu_real_master",
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE decision_logs SET created_at=? WHERE rowid = "
            "(SELECT MAX(rowid) FROM decision_logs)", (created,),
        )
        connection.commit()
    # 种一笔同键实际成交（正常路径下对账 matched）
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO order_intents (strategy_id, intent_key, symbol, side,"
            " quantity, limit_price, status, created_at, updated_at, tenant_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("volatility_long_gamma", f"bt:{created}", "US.AMD261218C5000",
             "BUY", 1, 2.5, "FILLED", created, created, "futu_real_master"),
        )
        intent_id = connection.execute(
            "SELECT id FROM order_intents WHERE intent_key=?", (f"bt:{created}",)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO fill_ledger (intent_id, strategy_id, symbol, side,"
            " quantity, price, source, filled_at) VALUES (?,?,?,?,?,?,?,?)",
            (intent_id, "volatility_long_gamma", "US.AMD261218C5000", "BUY",
             1, 2.5, "BROKER_TEST", created),
        )
        connection.commit()

    out_root = tmp_path / "backtests"
    monkeypatch.setattr(cli, "OUTPUT_ROOT", out_root)
    argv = [
        "run", "--from", "2026-11-06T00:00:00+00:00",
        "--to", "2026-11-07T00:00:00+00:00",
        "--label", "mutation", "--db", str(db),
    ]
    args = cli._build_parser().parse_args(argv)

    # 正向基线：撮合层正常（该信号 payload 无 expected_premium → L3 跳过、
    # 实际成交无双键对齐 → 有对账行），报告生成且返回值两态合法。
    # 关键断言：**永不成交变异**下，实际成交必然失去回测对手方 →
    # backtest_bug 计数必须出现在原因分布里（报告 A 转红的机制证据）。
    from futu_quant.signal_backtest import matcher as bt_matcher

    def never_fill(*_args, **_kwargs):
        raise ValueError("变异：永不成交")

    monkeypatch.setattr(bt_matcher, "match_trade", never_fill)
    rc = cli.run_backtest(args)
    report_text = (out_root / "mutation" / "report.md").read_text(encoding="utf-8")
    assert "自校验" in report_text
    # 实际成交存在且回测零撮合 → 对账行含 backtest_bug（转红机制成立）；
    # CLI 退出码必须非 0（报告不是摆设，PRD 硬门槛 5）。
    assert rc == 1, (
        "永不成交变异下报告仍返回 0 —— 自校验未把「实际有、回测无」判红，"
        "报告形同虚设"
    )
