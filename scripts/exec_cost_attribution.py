"""成交成本归因报表（G3，只读）。

背景（2026-10-25 Chief 排期）：3 个月 712 笔成交、费用约 $788、82 笔 REJECTED、
13 笔 FAILED、40 笔 CANCELLED **从未归因**，滑点也从未统计。这块回报确定、与 alpha 无关。

本脚本**只读**：不写库、不下单、不改开关。产出 markdown 报告到 `data/qa/`。

用法：
    python scripts/exec_cost_attribution.py                 # 打印摘要
    python scripts/exec_cost_attribution.py --out data/qa/exec_cost_attribution.md
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "trading.db"

# 失败状态归因口径：REJECTED 多为门禁拦下（可解释），FAILED 多为执行失败（要追）
_FILLED = "FILLED"
_FAILURE_STATUSES = ("REJECTED", "FAILED", "CANCELLED", "CANCELLED_ALL")


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _print_safe(*args, **kwargs) -> None:
    """打印容忍非 UTF-8 控制台（Windows GBK 默认，emoji 会 UnicodeEncodeError）。"""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        fallback = " ".join(str(arg) for arg in args)
        print(fallback.encode("ascii", "replace").decode("ascii"), **kwargs)


# 失败归因分桶（固定口径，避免每次报告各写一套）：
# REJECTED 多为门禁拦下（可解释），FAILED 多为执行失败（要追）
FAILURE_BUCKETS: dict[str, tuple[str, ...]] = {
    "限额": ("名义金额", "限额", "额度", "max_daily", "max_order"),
    "急停": ("急停", "熔断", "kill switch"),
    "对账": ("对账", "三账", "归属", "reconciliation"),
    "监管": ("监管", "中国内地", "解锁交易", "权限"),
    "持仓资金": ("持仓不足", "资金", "可卖", "购买力"),
    "其他": (),
}


def classify_failure(reason: str | None) -> str:
    """把失败原因文本归类到固定桶；空原因文本单独一桶。"""
    text = str(reason or "").strip()
    if not text or text == "(无错误文本)":
        return "无原因文本"
    lowered = text.lower()
    for bucket, keywords in FAILURE_BUCKETS.items():
        for keyword in keywords:
            if keyword.lower() in lowered:
                return bucket
    return "其他"


def fee_reconciliation(connection: sqlite3.Connection) -> dict:
    """两个费用来源对账：意图级 `order_intents.fees` vs 成交级 `fill_ledger.fee_total`。

    两者口径不同（前者按意图、后者按成交），**必须同时报出并给差异**，
    否则任何一个来源单独看都可能把成本说小一半（实测差约 $1000）。

    额外做**可靠性判定**（2026-10-25 实测根因）：
    `fill_ledger.fee_total` 有 689/743 行**恒等于 1.0**（占位/最低值，不是真实费用），
    而 `order_intents.fees` 与 `fee_model.estimate_fees`（全费用估算）在 709/742 条上
    误差 <$0.02。结论：**fill_ledger 的费用总额不可作为「成本」引用**，
    直到券商 commission 真正落库；当前最接近真实的是 estimate（intents.fees）。
    """
    intent_total = connection.execute(
        "SELECT COALESCE(SUM(CAST(fees AS REAL)), 0), "
        "COUNT(NULLIF(CAST(fees AS REAL), 0)) FROM order_intents"
    ).fetchone()
    ledger_total = connection.execute(
        "SELECT COALESCE(SUM(COALESCE(fee_total, 0)), 0), "
        "COUNT(NULLIF(fee_total, 0)) FROM fill_ledger"
    ).fetchone()
    top_value, top_count = connection.execute(
        "SELECT ROUND(COALESCE(fee_total,0),4) AS v, COUNT(*) c FROM fill_ledger "
        "GROUP BY v ORDER BY c DESC LIMIT 1"
    ).fetchone() or (None, 0)
    ledger_rows = connection.execute("SELECT COUNT(*) FROM fill_ledger").fetchone()[0]
    delta = round(float(intent_total[0]) - float(ledger_total[0]), 2)
    placeholder_ratio = (top_count / ledger_rows) if ledger_rows else 0.0
    ledger_reliable = not (top_count >= max(10, ledger_rows * 0.5))
    return {
        "intent_fees_total": round(float(intent_total[0]), 2),
        "intents_with_fees": int(intent_total[1]),
        "ledger_fee_total": round(float(ledger_total[0]), 2),
        "ledger_rows_with_fees": int(ledger_total[1]),
        "delta": delta,
        "consistent": abs(delta) <= 0.01,
        "ledger_modal_fee_value": float(top_value) if top_value is not None else None,
        "ledger_modal_fee_rows": int(top_count),
        "ledger_modal_fee_ratio": round(placeholder_ratio, 4),
        "ledger_reliable": ledger_reliable,
        "authoritative": (
            "intents.fees（estimate_fees 全费用估算，与 709/742 条误差 <$0.02）"
            if not ledger_reliable else "fill_ledger.fee_total"
        ),
        "note": (
            "两个来源口径不同（意图级 vs 成交级）；"
            + (
                f"且 fill_ledger 有 {top_count}/{ledger_rows} 行 fee_total 恒为 {top_value}"
                "（占位值），**成交级总额不可当成本引用**；"
                if not ledger_reliable else ""
            )
            + "差异非 0 时不得只取其一当唯一答案，需判断哪一侧漏记（本报告同时展示）"
        ),
    }


def cost_summary(connection: sqlite3.Connection) -> dict:
    """成交成本：手续费 / 名义金额 / 单位成本（bps）。

    口径铁律（2026-10-25 冻结，Reviewer 实测复核）：
    **成本 = `order_intents.fees`**（`estimate_fees` 全费用估算）。
    `fill_ledger.fee_total` 有 689/743 行为 $1.0 占位值（合计 $787.99），
    只能作对比基线，**不得当成本引用**。估算 ≠ 实付：`commission` 列全 0，
    真实手续费需券商对账单。
    """
    row = connection.execute(
        """
        SELECT COUNT(*) AS fills,
               ROUND(SUM(ABS(price * quantity * COALESCE(multiplier, 1))), 2) AS notional,
               ROUND(SUM(COALESCE(fee_total, 0)), 2) AS ledger_fees,
               ROUND(SUM(COALESCE(commission, 0)), 2) AS commission
        FROM fill_ledger
        """
    ).fetchone()
    fills, notional, ledger_fees, commission = row
    authoritative = float(
        connection.execute(
            "SELECT COALESCE(SUM(CAST(fees AS REAL)), 0) FROM order_intents"
        ).fetchone()[0]
        or 0.0
    )
    authoritative_filled = float(
        connection.execute(
            "SELECT COALESCE(SUM(CAST(fees AS REAL)), 0) FROM order_intents "
            "WHERE UPPER(status) = 'FILLED'"
        ).fetchone()[0]
        or 0.0
    )
    # bps / 单笔成本口径必须与分母对齐：分子用 FILLED-only，
    # 分母用 fill_ledger 名义额（743 笔成交），否则「882 条意图的费 ÷ 743 笔的名义额」口径不配对
    fee_bps = (authoritative_filled / notional * 10000) if notional else None
    fee_bps_all_intents = (authoritative / notional * 10000) if notional else None
    ledger_bps = (ledger_fees / notional * 10000) if notional else None
    fee_per_trade = (authoritative_filled / fills) if fills else None
    return {
        "fills": fills or 0,
        "notional": notional or 0.0,
        # 权威口径：全费用估算（含未成交意图的预估费，仅用于每日成本）
        "fees": round(authoritative, 2),
        "fees_filled_only": round(authoritative_filled, 2),
        "fee_bps": round(fee_bps, 2) if fee_bps is not None else None,
        "fee_bps_all_intents": (
            round(fee_bps_all_intents, 2) if fee_bps_all_intents is not None else None
        ),
        "fee_per_trade": round(fee_per_trade, 2) if fee_per_trade is not None else None,
        "fees_are_estimate": True,
        # 对比基线：成交级账本，含占位值，不可引用
        "ledger_fees": ledger_fees or 0.0,
        "ledger_fee_bps": round(ledger_bps, 2) if ledger_bps is not None else None,
        "commission": commission or 0.0,
    }


def slippage_summary(connection: sqlite3.Connection) -> dict:
    """滑点：成交价 vs 参考价（reference_price）。

    注意：`spread_cost` / `price_impact_cost` 两列**有值但恒为 0**（743/743 非空、合计 0.0），
    说明这两项从未真正计算过 —— 所以这里用 reference_price 反推，并在报告里点名该缺陷。
    方向：BUY 成交价高于参考价 = 不利（正成本），SELL 反之。
    """
    rows = connection.execute(
        """
        SELECT strategy_id, side, price, reference_price, quantity, COALESCE(multiplier, 1) AS mult
        FROM fill_ledger
        WHERE reference_price IS NOT NULL AND reference_price > 0 AND price > 0
        """
    ).fetchall()
    total_usd = 0.0
    samples = 0
    worse = 0
    bps_values: list[float] = []
    by_strategy: dict[str, dict] = {}
    for strategy_id, side, price, ref, qty, mult in rows:
        diff = (price - ref) if str(side).upper() == "BUY" else (ref - price)
        usd = diff * qty * mult
        total_usd += usd
        samples += 1
        if usd > 0:
            worse += 1
        bps = (price - ref) / ref * 10000.0 * (1 if str(side).upper() == "BUY" else -1)
        bps_values.append(bps)
        bucket = by_strategy.setdefault(
            str(strategy_id or "?"), {"samples": 0, "total_usd": 0.0, "worse": 0, "bps": []}
        )
        bucket["samples"] += 1
        bucket["total_usd"] += usd
        bucket["worse"] += int(usd > 0)
        bucket["bps"].append(bps)

    def _stat(values: list[float]) -> dict:
        if not values:
            return {"n": 0, "mean_bps": None, "median_bps": None, "p90_bps": None}
        ordered = sorted(values)

        def _q(q: float) -> float:
            pos = (len(ordered) - 1) * q
            lower = int(pos)
            upper = min(lower + 1, len(ordered) - 1)
            return ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower)

        return {
            "n": len(values),
            "mean_bps": round(sum(values) / len(values), 2),
            "median_bps": round(_q(0.5), 2),
            "p90_bps": round(_q(0.9), 2),
        }

    return {
        "samples": samples,
        "total_usd": round(total_usd, 2),
        "avg_usd": round(total_usd / samples, 4) if samples else None,
        "worse_than_ref": worse,
        "worse_ratio": round(worse / samples, 4) if samples else None,
        "bps": _stat(bps_values),
        "by_strategy": {
            strategy: {
                "samples": item["samples"],
                "total_usd": round(item["total_usd"], 2),
                "worse_ratio": round(item["worse"] / item["samples"], 4) if item["samples"] else None,
                **_stat(item["bps"]),
            }
            for strategy, item in sorted(by_strategy.items())
        },
    }


def dead_column_check(connection: sqlite3.Connection) -> list[str]:
    """找出「有值但恒为 0」的成本列 —— 这类列会让人误以为在统计。"""
    problems = []
    for col in ("spread_cost", "price_impact_cost", "commission"):
        non_null, non_zero = connection.execute(
            f"SELECT COUNT({col}), COUNT(NULLIF({col}, 0)) FROM fill_ledger"
        ).fetchone()
        if non_null and non_zero == 0:
            problems.append(f"{col}：{non_null} 行有值但**全部为 0**，等于从未统计")
    if not problems:
        non_null = connection.execute(
            "SELECT COUNT(bid_price) FROM fill_ledger"
        ).fetchone()[0]
        if non_null == 0:
            problems.append("bid_price/ask_price/mid_price：**全表为空**，无法按盘口口径复核滑点")
    return problems


def failure_summary(connection: sqlite3.Connection) -> dict:
    """失败/取消归因：按状态 × 错误文本聚合。"""
    counts = {}
    for status, c in connection.execute(
        "SELECT status, COUNT(*) FROM order_intents GROUP BY status"
    ):
        counts[status] = c
    reasons = []
    placeholders = ",".join("?" * len(_FAILURE_STATUSES))
    for status, err, c in connection.execute(
        f"""
        SELECT status, substr(COALESCE(NULLIF(error, ''), '(无错误文本)'), 1, 120) AS err,
               COUNT(*) AS c
        FROM order_intents
        WHERE status IN ({placeholders})
        GROUP BY status, err
        ORDER BY c DESC
        """,
        _FAILURE_STATUSES,
    ):
        reasons.append({"status": status, "reason": err, "count": c})
    buckets: dict[str, int] = {}
    for item in reasons:
        bucket = classify_failure(item["reason"])
        buckets[bucket] = buckets.get(bucket, 0) + item["count"]
    total = sum(buckets.values())
    return {
        "counts": counts,
        "reasons": reasons,
        "by_bucket": dict(sorted(buckets.items(), key=lambda kv: -kv[1])),
        "share_by_bucket": {
            bucket: count / total for bucket, count in buckets.items()
        } if total else {},
    }


def daily_fees(connection: sqlite3.Connection) -> list[dict]:
    """每日费用：权威估算（intents）与账本名义（fill_ledger）并列。"""
    ledger = {
        day: float(fee or 0.0)
        for day, fee in connection.execute(
            "SELECT substr(filled_at, 1, 10) AS d, SUM(COALESCE(fee_total, 0)) "
            "FROM fill_ledger GROUP BY d"
        )
    }
    intents = {
        day: float(fee or 0.0)
        for day, fee in connection.execute(
            "SELECT substr(created_at, 1, 10) AS d, SUM(CAST(fees AS REAL)) "
            "FROM order_intents GROUP BY d"
        )
    }
    return [
        {
            "day": day,
            "authoritative": round(intents.get(day, 0.0), 2),
            "ledger": round(ledger.get(day, 0.0), 2),
            # 有成交的交易日才进分母（$0 手续费的成交日也是交易日）
            "has_fills": day in ledger,
        }
        for day in sorted(set(ledger) | set(intents))
    ]


def render(db_path: Path) -> tuple[str, dict]:
    connection = _connect(db_path)
    try:
        cost = cost_summary(connection)
        fee_recon = fee_reconciliation(connection)
        slip = slippage_summary(connection)
        dead = dead_column_check(connection)
        failures = failure_summary(connection)
        fees_by_day = daily_fees(connection)
    finally:
        connection.close()

    total_intents = sum(failures["counts"].values())
    filled = failures["counts"].get(_FILLED, 0)
    fill_rate = (filled / total_intents) if total_intents else None
    trade_days = len({item["day"] for item in fees_by_day if item["has_fills"]})
    fee_per_day = (cost["fees"] / trade_days) if trade_days else None
    ledger_fee_per_day = (cost["ledger_fees"] / trade_days) if trade_days else None

    lines = [
        "# 成交成本与失败归因（G3，只读）",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"数据源：`{db_path}`",
        "",
        "## 1. 成交成本",
        "",
        f"- 成交笔数：**{cost['fills']}**，覆盖 **{trade_days}** 个交易日",
        f"- 名义成交额：**${cost['notional']:,.2f}**",
        f"- **权威费用（`order_intents.fees`，全部意图）**：**${cost['fees']:,.2f}**"
        + (f"（约 **${fee_per_day:,.2f}/交易日**）" if fee_per_day is not None else ""),
        "  - 性质：**全费用估算，非已扣款**（estimate_fees；`commission` 全 0，真实值需券商对账单）",
        f"  - 其中 **FILLED 意图口径**（bps/单笔成本与 743 笔成交分母对齐）："
        f"**${cost['fees_filled_only']:,.2f}**"
        + (f"，**{cost['fee_bps']:.2f} bps**" if cost["fee_bps"] is not None else "")
        + (f"，**${cost['fee_per_trade']:.2f}/笔**" if cost["fee_per_trade"] is not None else "")
        + "（未成交意图的预估费不计入实际支出）",
        f"- 对比基线（`fill_ledger.fee_total`，**不可引用**）：**${cost['ledger_fees']:,.2f}**"
        + (f"（约 ${ledger_fee_per_day:,.2f}/交易日）" if ledger_fee_per_day is not None else "")
        + (f"，{cost['ledger_fee_bps']} bps" if cost["ledger_fee_bps"] is not None else "")
        + " —— 含占位值，仅留作修复写路径后的对比",
        "",
        "### 1.1 两个费用来源对账（必须同时看）",
        "",
        f"- 意图级 `order_intents.fees` 合计：**${fee_recon['intent_fees_total']:,.2f}**"
        f"（{fee_recon['intents_with_fees']} 条）",
        f"- 成交级 `fill_ledger.fee_total` 合计：**${fee_recon['ledger_fee_total']:,.2f}**"
        f"（{fee_recon['ledger_rows_with_fees']} 行）",
        f"- 差异：**${fee_recon['delta']:,.2f}**"
        + ("（一致）" if fee_recon["consistent"] else "（**不一致，需查哪一侧漏记**）"),
        f"- 口径说明：{fee_recon['note']}",
        (
            f"- **可靠性判定**：`fill_ledger.fee_total` 众数值 **{fee_recon['ledger_modal_fee_value']}**"
            f"（{fee_recon['ledger_modal_fee_rows']} 行 / 占 {fee_recon['ledger_modal_fee_ratio']:.1%}）→ "
            + (
                "**成交级总额不可作为成本引用**（占位值）"
                if not fee_recon["ledger_reliable"] else "成交级总额可用"
            )
        ),
        f"- 当前权威口径：**{fee_recon['authoritative']}**",
        "",
        "## 2. 滑点：UNKNOWN（不下结论）",
        "",
        "> ⚠️ 本节数字**仅诊断参考，不采纳为成本、不进 G4 仓位公式**：",
        "> `reference_price` 时点未定义（限价？中价？），`spread_cost`/`price_impact_cost` 全 0，",
        "> `bid/ask/mid` 全为空 → 无法当市场冲击。口径定义并经 Reviewer 确认后才可引用。",
        "",
        f"- 可比样本：**{slip['samples']}** 笔",
        (
            f"- 累计滑点成本：**${slip['total_usd']:,.2f}**，均值 **${slip['avg_usd']}**/笔"
            if slip["samples"] else "- 无可比样本"
        ),
        (
            f"- 劣于参考价的比例：**{slip['worse_ratio']:.1%}**（{slip['worse_than_ref']}/{slip['samples']} 笔）"
            if slip["samples"] else ""
        ),
        (
            f"- 不利滑点 bps：均值 **{slip['bps']['mean_bps']}** / 中位 **{slip['bps']['median_bps']}**"
            f" / P90 **{slip['bps']['p90_bps']}**"
            if slip["samples"] else ""
        ),
        "",
    ]
    if slip["by_strategy"]:
        lines.extend([
            "### 2.1 分策略滑点",
            "",
            "| 策略 | 样本 | 累计USD | 劣于参考价占比 | 均值bps | 中位bps | P90bps |",
            "|---|---|---|---|---|---|---|",
        ])
        for strategy, item in slip["by_strategy"].items():
            lines.append(
                f"| {strategy} | {item['samples']} | {item['total_usd']:,.2f} | "
                f"{(item['worse_ratio'] or 0):.1%} | {item['mean_bps']} | "
                f"{item['median_bps']} | {item['p90_bps']} |"
            )
        lines.append("")
    lines.extend([
        "## 3. 成本列健康度（防「以为在统计」）",
        "",
    ])
    if dead:
        lines.extend(f"- ⚠️ {item}" for item in dead)
    else:
        lines.append("- 全部成本列均有真实数值")

    lines.extend([
        "",
        "## 4. 失败 / 取消归因",
        "",
        f"- 订单意图总数：**{total_intents}**，成交 **{filled}** 笔"
        + (f"，成交率 **{fill_rate:.1%}**" if fill_rate is not None else ""),
        "",
        "| 状态 | 笔数 |",
        "|---|---|",
    ])
    for status, c in sorted(failures["counts"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {status} | {c} |")
    lines.extend(["", "### 4.1 失败原因分桶", "", "| 归因桶 | 次数 | 占比 |", "|---|---|---|"])
    for bucket, count in failures.get("by_bucket", {}).items():
        share = failures.get("share_by_bucket", {}).get(bucket, 0.0)
        lines.append(f"| {bucket} | {count} | {share:.1%} |")
    lines.extend(["", "### 原因明细（按次数降序）", "", "| 状态 | 次数 | 原因 |", "|---|---|---|"])
    for item in failures["reasons"]:
        reason = str(item["reason"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['status']} | {item['count']} | {reason} |")

    lines.extend([
        "",
        "## 5. 每日费用（权威估算 vs 不可引用账本）",
        "",
        "| 交易日 | 权威估算(USD) | 账本名义(USD，占位，不可引用) |",
        "|---|---|---|",
    ])
    for item in fees_by_day:
        lines.append(f"| {item['day']} | {item['authoritative']:,.2f} | {item['ledger']:,.2f} |")

    return "\n".join(line for line in lines if line is not None) + "\n", {
        "cost": cost,
        "fee_reconciliation": fee_recon,
        "slippage": slip,
        "slippage_status": "UNKNOWN",
        "dead_columns": dead,
        "failures": failures,
        "trade_days": trade_days,
        "fill_rate": fill_rate,
        "fee_per_day": fee_per_day,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="成交成本与失败归因（只读）")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default="", help="报告输出路径（markdown）")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        _print_safe(f"数据库不存在：{db_path}")
        return 2

    report, summary = render(db_path)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        _print_safe(f"报告已写入：{out}")

    cost = summary["cost"]
    recon = summary["fee_reconciliation"]
    _print_safe(
        f"成交 {cost['fills']} 笔 / 权威费用（估算，非实付）${cost['fees']:,.2f}"
        f" / 约 ${summary['fee_per_day'] or 0:,.2f} 每交易日"
        f"（FILLED 口径 ${cost['fees_filled_only']:,.2f} / {cost['fee_bps']:.2f} bps"
        + (f" / ${cost['fee_per_trade']:.2f}/笔）" if cost["fee_per_trade"] is not None else "）")
    )
    _print_safe(
        f"费用两源：意图 ${recon['intent_fees_total']:,.2f} vs 成交 ${recon['ledger_fee_total']:,.2f}"
        f"（差 ${recon['delta']:,.2f}）"
    )
    _print_safe(
        f"费用可靠性：fill_ledger 众数 ${recon['ledger_modal_fee_value']} 占 "
        f"{recon['ledger_modal_fee_ratio']:.1%}（{recon['ledger_modal_fee_rows']} 行）→ "
        + ("成交级不可当成本" if not recon["ledger_reliable"] else "成交级可用")
        + f"；权威口径 {recon['authoritative']}"
    )
    if summary["slippage"]["samples"]:
        _print_safe(
            f"滑点 UNKNOWN（诊断参考，不采纳）：累计 ${summary['slippage']['total_usd']:,.2f}"
            f"（劣于参考价 {summary['slippage']['worse_ratio']:.1%}）"
        )
    for item in summary["dead_columns"]:
        _print_safe(f"⚠️ {item}")
    if summary["fill_rate"] is not None:
        _print_safe(f"成交率 {summary['fill_rate']:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
