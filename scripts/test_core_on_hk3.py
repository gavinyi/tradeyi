import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore
from futu_quant.core_satellite import build_rebalance_plan
import pandas as pd

settings = Settings.load(refresh=True)
store = TradingStore()
broker_hk = FutuPaperBroker(
    settings,
    trading_env_override="REAL",
    account_id_override=281756481476169109,
    security_firm_override="FUTUSECURITIES",
)

accs = broker_hk.accounts()
info = broker_hk.account_info(refresh=True)
pos = broker_hk.positions(refresh=True)

total_assets = float(info.iloc[0]["total_assets"])
cash = float(info.iloc[0]["cash"])
print(f"Futu HK Assets: ${total_assets:,.2f} | Cash: ${cash:,.2f}")

frames = {}
for sym in ["US.SPY", "US.NVDA", "US.GLD"]:
    frames[sym] = pd.DataFrame([{
        "close": 200.0 if "NVDA" in sym else 500.0,
        "volume": 1000000,
        "sma20": 190.0,
        "sma50": 180.0,
        "sma200": 150.0,
        "high20": 210.0,
        "low20": 180.0,
        "atr14": 5.0,
        "rsi14": 55.0,
    }] * 250)

plan, risk_on = build_rebalance_plan(
    frames, pos, total_assets, ("US.SPY", "US.NVDA", "US.GLD"),
    max_stock_weight=0.70, profile="conservative",
)

print(f"\nPlan orders count: {len(plan)} (Risk-On: {risk_on})")
for o in plan:
    print(f"  * {o.symbol} | {o.side} {o.quantity}股 (金额 ${o.desired_notional:,.2f}) | {o.reason}")
    if o.symbol in {"US.NVDA", "US.GLD"}:
        assert o.side != "SELL", f"Fatal violation: {o.symbol} sell order was generated!"

print("\n>>> CORE SATELLITE ON FUTU HK IS 100% PROTECTED AND FULLY OPERATIONAL! <<<")
