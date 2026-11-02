"""实盘「容量预检」实证探针（2026-11-02，Chief 指派先取证）。

只回答一个问题：**按当前实盘权益与 Product 定案的六条判定，今天的链上到底有没有
买得起、且流动性合格的合约？**

只读：只调 `option_expirations` / `option_chain` / `snapshots` / `account_info`，
**不下单、不写库、不改任何 campaign**。结论落到 `data/qa/`。

用法（生产机）：
    python scripts/probe_real_capacity_candidates.py --tenant futu_real_master
    python scripts/probe_real_capacity_candidates.py --max-symbols 4 --max-expiries 2
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from futu_quant.broker import FutuPaperBroker, close_all_connections  # noqa: E402
from futu_quant.capacity_precheck import (  # noqa: E402
    DEPTH_MIN_MULTIPLE, MAX_QUOTE_AGE_SECONDS, MAX_SPREAD_RATIO, evaluate_row,
    capacity_equity_line, minimum_premium,
)
from futu_quant.config import Settings  # noqa: E402
from futu_quant.config import DEFAULT_VOLATILITY_SYMBOLS  # noqa: E402
from futu_quant.tenant import resolve_tenant  # noqa: E402

MAX_SYMBOLS = 0
MAX_EXPIRIES = 2
MONEYNESS_WINDOW = 0.10


def _quotes_for(broker, codes: list[str]) -> pd.DataFrame:
    frames = []
    for start in range(0, len(codes), 200):
        frames.append(broker.snapshots(codes[start:start + 200]))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def probe_symbol(
    broker, symbol: str, spot: float, equity: float, max_expiries: int,
    min_dte: int = 5, max_dte: int = 21,
) -> dict:
    today = datetime.now().date()
    expirations = broker.option_expirations(symbol)
    if expirations is None or expirations.empty:
        return {"symbol": symbol, "error": "no expirations"}
    frame = expirations.copy()
    frame["expiry"] = pd.to_datetime(frame["strike_time"]).dt.date
    frame = frame[(frame["expiry"] >= today + timedelta(days=min_dte))
                  & (frame["expiry"] <= today + timedelta(days=max_dte))]
    frame = frame.sort_values("expiry").head(max_expiries)
    if frame.empty:
        return {"symbol": symbol, "error": "no expiry in DTE 5-21"}
    chains = []
    for expiry in frame["strike_time"]:
        chain = broker.option_chain(symbol, str(expiry))
        if chain is None or chain.empty:
            continue
        chain = chain.copy()
        chain["strike_price"] = pd.to_numeric(chain["strike_price"], errors="coerce")
        chains.append(chain)
    if not chains:
        return {"symbol": symbol, "error": "empty chain"}
    chain = pd.concat(chains, ignore_index=True)
    if spot > 0:
        chain = chain[(chain["strike_price"] / spot - 1).abs() <= MONEYNESS_WINDOW]
    if chain.empty:
        return {"symbol": symbol, "error": "no strikes near spot"}
    quotes = _quotes_for(broker, chain["code"].astype(str).tolist())
    if quotes is None or quotes.empty:
        return {"symbol": symbol, "error": "no quotes"}
    merged = chain.merge(quotes, on="code", suffixes=("", "_quote"))
    # 同一个 code 可能因多到期日/多来源重复出现，**必须去重**，否则「合格张数」会虚高。
    merged = merged.drop_duplicates(subset=["code"])
    if "suspension" in merged.columns:
        merged = merged[~merged["suspension"].fillna(False).astype(bool)]
    if "option_open_interest" in merged.columns:
        # 与生产选约同源的最低持仓量门槛（`candidate_quality` 默认 500）
        merged = merged[pd.to_numeric(merged["option_open_interest"], errors="coerce").fillna(0) >= 500]
    passed: list[dict] = []
    reasons: dict[str, int] = {}
    cheapest = None
    for _, row in merged.iterrows():
        premium = minimum_premium(row)
        if premium is not None and (cheapest is None or premium < cheapest):
            cheapest = premium
        ok, reason, text, _premium = evaluate_row(row, equity=equity, spot=spot)
        if ok:
            passed.append({
                "code": str(row.get("code")),
                "option_type": str(row.get("option_type")),
                "ask": float(row.get("ask_price")),
                "premium": round(float(premium or 0), 2),
                "spread_pct": round(
                    (float(row["ask_price"]) - float(row["bid_price"]))
                    / ((float(row["ask_price"]) + float(row["bid_price"])) / 2) * 100, 2),
                "bid_vol": float(row.get("bid_vol") or 0),
                "ask_vol": float(row.get("ask_vol") or 0),
            })
        else:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "symbol": symbol,
        "spot": round(spot, 2),
        "contracts": int(len(merged)),
        "passed": int(len(passed)),
        "cheapest_premium": None if cheapest is None else round(cheapest, 2),
        "reasons": reasons,
        "examples": passed[:3],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default="futu_real_master")
    parser.add_argument("--max-symbols", type=int, default=0, help="0 = 全池")
    parser.add_argument("--max-expiries", type=int, default=MAX_EXPIRIES)
    parser.add_argument("--min-dte", type=int, default=5, help="到期下限（天）")
    parser.add_argument("--max-dte", type=int, default=21, help="到期上限（天）")
    parser.add_argument(
        "--symbols", default="",
        help="逗号分隔的标的（默认 = 默认波动率核心池；不读 DB，取证全程零写入）",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    settings = Settings.load()
    tenant = resolve_tenant(args.tenant, strict=True)
    broker = FutuPaperBroker(
        settings, trading_env_override=tenant.trading_env,
        account_id_override=tenant.account_id,
    )
    # 只用默认核心池：`load_volatility_symbols` 会建 `TradingStore()`（可能初始化/迁移库），
    # 取证不应有任何写风险；实盘另有已批准投放标的，这里取**更保守的子集**。
    symbols = ([item.strip() for item in args.symbols.split(",") if item.strip()]
               if args.symbols else list(DEFAULT_VOLATILITY_SYMBOLS))
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    account = broker.account_info()
    equity = float(account.iloc[0].get("total_assets", 0) or 0) if not account.empty else 0.0
    currency = str(account.iloc[0].get("currency") or "") if not account.empty else ""
    line = capacity_equity_line(equity)
    print(f"租户 {tenant.tenant_id} | 权益 ${equity:,.2f} ({currency}) | 容量线 ${line:,.2f}")
    print(f"标的池 {len(symbols)} 个：{', '.join(symbols)}")
    print(f"判定：价差 ≤{MAX_SPREAD_RATIO:.0%}、深度 ≥{DEPTH_MIN_MULTIPLE}×lot、"
          f"新鲜度 ≤{MAX_QUOTE_AGE_SECONDS:.0f}s、权利金 ≤ 容量线")

    rows = []
    for symbol in symbols:
        try:
            spot_frame = broker.snapshots([symbol])
            spot = float(spot_frame.iloc[0].get("last_price", 0) or 0) if not spot_frame.empty else 0.0
            row = probe_symbol(
                broker, symbol, spot, equity, args.max_expiries,
                args.min_dte, args.max_dte,
            )
        except Exception as exc:  # noqa: BLE001 - 单个标的失败不终止取证
            row = {"symbol": symbol, "error": str(exc)[:200]}
        rows.append(row)
        if "error" in row:
            print(f"  {symbol:10s} 跳过：{row['error']}")
        else:
            print(f"  {symbol:10s} 合约 {row['contracts']:3d} | 合格 {row['passed']:2d} | "
                  f"最便宜 ${row['cheapest_premium']} | 拒绝 {row['reasons']}")
    total_contracts = sum(row.get("contracts", 0) for row in rows)
    total_passed = sum(row.get("passed", 0) for row in rows)
    verdict = (
        f"今日 {len(rows)} 个标的共 {total_contracts} 张可选合约中，"
        f"满足容量线与流动性条件的 {total_passed} 张"
    )
    print("\n结论：" + verdict)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tenant": tenant.tenant_id,
        "equity": equity,
        "currency": currency,
        "capacity_line": round(line, 2),
        "criteria": {
            "max_spread_ratio": MAX_SPREAD_RATIO,
            "depth_min_multiple": DEPTH_MIN_MULTIPLE,
            "max_quote_age_seconds": MAX_QUOTE_AGE_SECONDS,
            "moneyness_window": MONEYNESS_WINDOW,
            "min_open_interest": 500,
            "max_expiries": args.max_expiries,
            "dte_window": [args.min_dte, args.max_dte],
        },
        "verdict": verdict,
        "symbols": rows,
    }
    out = Path(args.out) if args.out else (
        ROOT / "data" / "qa" /
        f"real_capacity_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已写入 {out}")
    close_all_connections()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
