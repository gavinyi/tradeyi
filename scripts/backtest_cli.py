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
from futu_quant.signal_backtest import optimizer as bt_optimizer  # noqa: E402
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

    # 对账（L-B）：与实际成交账本逐笔对照（两租户合并读，报告按窗口过滤）。
    # 数据可得性边界（2026-11-07 裁定）：撮合层产不出任何模拟成交（L3 100%
    # 历史窗口形态）→ 实际成交落 no_price_basis 独立档，不算 backtest_bug。
    fills = []
    for tenant in ("futu_real_master", "futu_paper_sandbox"):
        fills.extend(
            {
                "symbol": row.get("symbol"), "price": row.get("price"),
                "filled_at": row.get("filled_at"),
            }
            for row in store.fill_ledger_rows(tenant_id=tenant)
        )
    from futu_quant.signal_backtest.ledger import (
        decision_agreement, equity_curve, reconcile,
    )
    simulated_fill_available = bool(trades)  # 撮合层一单未出 = 无价格依据窗口
    reconcile_report = reconcile(trades, fills,
                                 simulated_fill_available=simulated_fill_available)
    curve = equity_curve(trades, fee_per_trade_fn=bt_matcher.fee_per_trade)

    # B1 决策结论对照（PRD §2 L-B 第一子项，无需价格数据）：重放放行 vs
    # 实际下单意图同向率——「为什么没成交」第一层数据，历史窗口现在就能算。
    intents = []
    for tenant in ("futu_real_master", "futu_paper_sandbox"):
        getter = getattr(store, "order_intents", None)
        if getter is None:
            break
        for row in getter(tenant_id=tenant):
            created = str(row.get("created_at") or "")
            if args.window_start <= created <= args.window_end:
                intents.append(row)
    agreement = decision_agreement(result.replayed_events, intents)

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
        # B1 三行判据（Product 2026-11-07）：方向分开看，不能只看同向率
        f"- **B1 · actual_only**：{agreement['actual_only_count']} 条"
        f"（实际下单、回放未放行——致命方向，必须≈0："
        f"{'✅ 达标' if agreement['actual_only_count'] == 0 else '❌ 回放判据比生产宽松，不得收官'}）",
        f"- **B1 · replay_only**：{agreement['replay_only_count']} 条"
        f"（回放放行、实际未下单——「为什么没成交」答案，初步归因分布见明细）",
        f"- **B1 · 未归因**：{agreement['unattributed_count']} 条"
        f"（{'✅ 0 条' if agreement['unattributed_count'] == 0 else '❌ 归不了的差异 = 回放器漏判，不得收官'}）",
        f"- B1 同向率（参考）：{agreement['agreement_ratio']:.2%}"
        f"（重放放行 {len(result.replayed_events)} 笔中 {agreement['both_count']} 笔有对应实际下单意图）",
        f"- 成交价格对照：{f'{reconcile_report.price_reconcile_ratio:.2%}' if reconcile_report.price_reconcile_ratio is not None else '**不适用**'}"
        f"（{f'对照 {len(reconcile_report.price_comparison_eligible_rows())} 笔' if reconcile_report.price_reconcile_ratio is not None else '本区间无价格依据（no_price_basis），不进对账率——禁止读作「对账通过」'}）",
        f"- no_price_basis 档：{reconcile_report.no_price_basis_count} 笔"
        f"（v2 盘口 11-06 起积累，历史窗口无价格依据，随 v2 积累后自动启用）",
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


def _evaluate_arm_factory(store, slippage: int):
    """构造组合评估器：复用 replay/matcher/ledger 既有纯函数（寻优不新增撮合口径）。

    一组参数 → 该参数在窗口内的重放（replay_signals）→ L2 撮合 → 含费净值。
    与 run_backtest 的撮合口径逐行同源（PRD 2.2 第 5 条：成本计入）。
    """
    from futu_quant.signal_backtest.ledger import equity_curve

    def evaluate_arm(params: dict, window_start: str, window_end: str):
        config = {"thresholds": {
            "entry_zscore": float(params.get("entry_zscore", 2.0)),
            "minimum_underlying_move": float(params.get("minimum_move", 0.02)),
        }}
        result = bt_replay.replay_signals(store, window_start, window_end, config)
        trades = []
        for event in result.replayed_events:
            premium = (event.payload.get("regime") or {}).get("expected_premium")
            quote = bt_matcher.PriceQuote(
                bid_price=None, ask_price=None,
                recorded_premium=float(premium) if premium else None,
                tier="L2" if premium else "L3",
            )
            try:
                trades.append(bt_matcher.match_trade(event, quote, slippage_ticks=slippage))
            except ValueError:
                continue  # L3 拒造数：该组参数按「无可评估成交」计
        curve = equity_curve(trades, fee_per_trade_fn=bt_matcher.fee_per_trade)
        final_equity = curve[-1][1] if curve else 10_000.0
        return bt_optimizer.ArmResult(
            params=dict(params), trades=len(trades),
            pnl=round(final_equity - 10_000.0, 2),
            extra={"alignment": result.aligned},
        )

    return evaluate_arm


def run_optimize(args: argparse.Namespace) -> int:
    """寻优子命令（PRD 包2）：参数空间事前落盘 → 样本内寻优 → 样本外验证 → 衰减报告。

    只读铁律：不写 runtime_config / limit_profile / 任何配置（PRD 2.3 硬性禁止，
    AST 守卫钉死）；产物只有报告文件。
    """
    out_dir = _ensure_output_dir(args.label, args.force)
    store = TradingStore(args.db, read_only=True)

    axes: dict[str, list] = {"entry_zscore": args.entry_zscore_values,
                             "minimum_move": args.minimum_move_values}
    space = bt_optimizer.declare_parameter_space(
        axes, declared_at=f"{args.window_start}->{args.window_end}", name=args.label,
    )
    (out_dir / "parameter_space.json").write_text(
        bt_optimizer.space_declaration_json(space), encoding="utf-8",
    )

    train_start, train_end, verify_start, verify_end = bt_optimizer.split_window(
        args.window_start, args.window_end, train_ratio=args.train_ratio,
    )
    evaluate_arm = _evaluate_arm_factory(store, args.slippage)
    train_results = bt_optimizer.evaluate_grid(space, train_start, train_end, evaluate_arm)
    out_results = bt_optimizer.evaluate_grid(space, verify_start, verify_end, evaluate_arm)

    report = bt_optimizer.OptimizerReport(
        space=space, train_window=(train_start, train_end),
        verify_window=(verify_start, verify_end),
        train_results=train_results, out_of_sample=out_results,
        random_baseline_pnl=bt_optimizer.random_pick_baseline(space, train_results),
    )
    rec = report.recommendation()
    lines = [
        "# 参数寻优报告",
        "",
        f"- **第一屏结论（衰减）**: {report.decay_verdict()}",
        f"- **参数空间**: N={space.size} 组（事前声明，axes={json.dumps({k: list(v) for k, v in space.axes.items()}, ensure_ascii=False)}），声明落盘 parameter_space.json",
        f"- **窗口切分**: 训练 {train_start} → {train_end}（前 {args.train_ratio:.0%}）/ 验证 {verify_start} → {verify_end}",
        f"- **样本内最优**: {json.dumps(rec.get('params', {}), ensure_ascii=False)}，pnl {rec.get('train_pnl', '-')}（{rec.get('train_trades', '-')} 笔）",
        f"- **样本外重跑**: pnl {rec.get('oos_pnl', '-')}（{rec.get('oos_trades', '-')} 笔），衰减率 {f'{report.decay_ratio:.0%}' if report.decay_ratio is not None else 'N/A'}",
        f"- **随机基准**（N 组里随机挑一组的期望 pnl）: {report.random_baseline_pnl}",
        f"- **推荐资格**: {'✅ 可进入推荐位' if rec.get('eligible') else '❌ ' + str(rec.get('reason'))}",
        f"- **交付完整性**: {'✅ 含样本外列' if report.is_deliverable else '❌ 缺样本外列（PRD 2.3：视为未交付）'}",
        "",
        "## 产品红线",
        "",
        "1. 寻优只出报告，不写任何生产配置——改参数必须人工审批；",
        "2. 回测/寻优结论不得用于放宽风控/质量门槛；",
        "3. 衰减 >60% = 过拟合，不建议采用。",
    ]
    report_path = out_dir / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"寻优报告已写入 {report_path}")
    print(report.decay_verdict())
    return 0 if report.is_deliverable else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="历史信号回测 CLI（只读）")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="跑一次回测并生成报告")
    _window_args(run_parser)
    opt_parser = sub.add_parser("optimize", help="参数寻优（样本内寻优+样本外验证+衰减报告）")
    _window_args(opt_parser)
    opt_parser.add_argument("--entry-zscore-values", type=float, nargs="+",
                            default=[1.8, 2.0, 2.2, 2.5], help="入场 Z 阈值取值集（事前声明）")
    opt_parser.add_argument("--minimum-move-values", type=float, nargs="+",
                            default=[0.015, 0.02, 0.03], help="最小涨跌取值集（事前声明）")
    opt_parser.add_argument("--train-ratio", type=float, default=0.7, help="训练段占比")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "optimize":
        return run_optimize(args)
    return run_backtest(args)


if __name__ == "__main__":
    raise SystemExit(main())
