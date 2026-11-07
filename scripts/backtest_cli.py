"""回测 CLI：`python scripts/backtest_cli.py run --from --to --label ...`。

只读铁律：以 `TradingStore(read_only=True)` 打开库；产出只有报告文件
（`data/backtests/<label>/report.md` + trades.csv），不写库、不碰下单路径。
输出目录按 label 隔离；重复跑同 label 需 `--force`（防覆盖，Chief 要求）。

报告头部固定四行（PRD §5 + Chief 补充）：
数据区间 / 配置快照版本 / 自校验是否通过 / 数据完整性声明。
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from futu_quant.signal_backtest import matcher as bt_matcher  # noqa: E402
from futu_quant.signal_backtest import replay as bt_replay  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402

OUTPUT_ROOT = Path("data/backtests")


def _window_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="window_start", required=True, help="窗口起点 ISO 时间")
    parser.add_argument("--to", dest="window_end", required=True, help="窗口终点 ISO 时间")
    parser.add_argument("--label", required=True, help="报告输出目录名")
    parser.add_argument("--db", default="data/trading.db", help="目标库（默认生产库，只读打开）")
    parser.add_argument("--entry-zscore", type=float, default=2.0)
    parser.add_argument("--minimum-move", type=float, default=0.02)
    parser.add_argument("--slippage", type=int, default=bt_matcher.DEFAULT_SLIPPAGE_TICKS)
    parser.add_argument("--force", action="store_true", help="覆盖已存在的同名 label 输出")


def _ensure_output_dir(label: str, force: bool) -> Path:
    out = OUTPUT_ROOT / label
    if out.exists() and any(out.iterdir()) and not force:
        raise SystemExit(
            f"输出目录已存在且非空：{out} —— 重复跑请加 --force（防覆盖）"
        )
    out.mkdir(parents=True, exist_ok=True)
    return out


def _config_snapshot(store) -> str:
    """配置快照版本标识（报告头第 2 行）：关键阈值键的当前值指纹。"""
    keys = (
        "risk_drawdown_warn_pct", "risk_drawdown_stop_pct",
        "CAPACITY_EQUITY_RATIO_MARKER",  # 占位：容量常量在代码层，报告注明
    )
    payload = {
        key: store.get_setting(key, "") for key in keys if key != "CAPACITY_EQUITY_RATIO_MARKER"
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def run_backtest(args: argparse.Namespace) -> int:
    out_dir = _ensure_output_dir(args.label, args.force)
    store = TradingStore(args.db, read_only=True)
    config = {"thresholds": {
        "entry_zscore": args.entry_zscore,
        "minimum_underlying_move": args.minimum_move,
    }}

    result = bt_replay.replay_signals(store, args.window_start, args.window_end, config)
    integrity = bt_replay.audit_log_integrity(store, args.window_start, args.window_end)
    self_check_passed = result.aligned  # L-A 不绿，报告 B 结论一律无效（PRD 红线 2）

    # 撮合：v1 以 L2（决策/账本记录权利金）为主——v2 盘口快照 11-06 起才有，
    # 历史窗口 L1 命中率低；每条信号的报价由 option_history / payload 提供，
    # 无价格依据的信号计入 L3 占比（不编造）。
    trades = []
    skipped_l3 = 0
    for event in result.replayed_events:
        premium = (event.payload.get("regime") or {}).get("expected_premium")
        quote = bt_matcher.PriceQuote(
            bid_price=None, ask_price=None,
            recorded_premium=float(premium) if premium else None,
            tier="L2" if premium else "L3",
        )
        try:
            trades.append(bt_matcher.match_trade(event, quote, slippage_ticks=args.slippage))
        except ValueError:
            skipped_l3 += 1  # L3 拒造数，仅计数进占比
    shares = bt_matcher.tier_shares(trades)
    total_signals = len(trades) + skipped_l3
    if total_signals:
        shares = {
            tier: round(count / total_signals, 4)
            for tier, count in {**shares, "L3": skipped_l3}.items()
        }

    # 对账（L-B）：与实际成交账本逐笔对照（两租户合并读，报告按窗口过滤）
    fills = []
    for tenant in ("futu_real_master", "futu_paper_sandbox"):
        fills.extend(
            {
                "symbol": row.get("symbol"), "price": row.get("price"),
                "filled_at": row.get("filled_at"),
            }
            for row in store.fill_ledger_rows(tenant_id=tenant)
        )
    from futu_quant.signal_backtest.ledger import reconcile, equity_curve  # 局部导入保持启动轻
    reconcile_report = reconcile(trades, fills)
    curve = equity_curve(trades, fee_per_trade_fn=bt_matcher.fee_per_trade)

    header = (
        f"- **数据区间**: {args.window_start} → {args.window_end}\n"
        f"- **配置快照**: {_config_snapshot(store)}\n"
        f"- **自校验是否通过**: {'✅ 通过' if self_check_passed else '❌ 未通过（报告 B 结论一律无效）'}\n"
        f"- **数据完整性**: 跨度 {integrity['span_start']} → {integrity['span_end']}，"
        f"窗口内小时缺口 {integrity.get('gap_count', 0)} 个\n"
        f"- **撮合分层占比**: L1 {shares.get('L1', 0):.0%} / "
        f"L2 {shares.get('L2', 0):.0%} / L3 {shares.get('L3', 0):.0%}"
        f"（v2 盘口 11-06 起积累，历史区间以 L2 为主，精度随 v2 积累提高）\n"
    )
    lines = [
        "# 回测报告",
        "",
        header.rstrip(),
        "",
        "## 报告 A · 自校验",
        "",
        f"- 信号对齐率：{result.alignment_ratio:.2%}"
        f"（实际 {len(result.actual_events)} / 重放 {len(result.replayed_events)}"
        f" / 差异 {len(result.mismatches)}）——判据 100%（PRD L-A）",
        f"- 成交对账率：{reconcile_report.reconcile_ratio:.2%}"
        f"（对照 {len(reconcile_report.rows)} 笔，目标 ≥90%）",
        f"- 差异原因分布：{json.dumps(reconcile_report.reason_distribution(), ensure_ascii=False)}",
        f"- **backtest_bug 占比 >0 整体标红**："
        f"{'❌ 有' if reconcile_report.has_backtest_bug else '✅ 无'}",
        "",
        "## 报告 C · 净值曲线（含费）",
        "",
        "```",
        *(f"{point[0]}  {point[1]:,.2f}" for point in curve[-30:]),
        "```",
        "",
        "## 产品红线提示",
        "",
        "回测结论不得用于放宽任何风控/质量门槛；只能回答「换个参数在同段历史里是否更好」。",
    ]

    report_path = out_dir / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    trades_path = out_dir / "trades.csv"
    with trades_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["created_at", "symbol", "price_tier", "fill_price", "premium_notional"])
        for trade in trades:
            writer.writerow([
                trade.created_at, trade.symbol, trade.price_tier,
                trade.fill_price, trade.premium_notional,
            ])
    print(f"报告已写入 {report_path} 与 {trades_path}")
    print(f"自校验：{'PASS' if self_check_passed else 'FAIL'}；"
          f"对账率 {reconcile_report.reconcile_ratio:.2%}；"
          f"L3 占比 {shares.get('L3', 0):.0%}")
    return 0 if self_check_passed and not reconcile_report.has_backtest_bug else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="历史信号回测 CLI（只读）")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="跑一次回测并生成报告")
    _window_args(run_parser)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return run_backtest(args)


if __name__ == "__main__":
    raise SystemExit(main())
