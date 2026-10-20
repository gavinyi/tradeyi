from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore
from futu_quant.risk import RiskLimits

settings = Settings.load(refresh=True)
store = TradingStore()
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)

print("=== 1. REAL ACCOUNT BALANCE & ASSETS ===")
info = broker_real.account_info(refresh=True)
if not info.empty:
    r = info.iloc[0]
    total_assets = float(r.get("total_assets", 0))
    cash = float(r.get("cash", 0))
    mv = float(r.get("market_val", r.get("securities_assets", 0)))
    power = float(r.get("power", 0))
    print(f"Total Assets: ${total_assets:,.2f}")
    print(f"Cash:         ${cash:,.2f} ({cash/total_assets*100:.1f}%)")
    print(f"Securities:   ${mv:,.2f} ({mv/total_assets*100:.1f}%)")
    print(f"Buying Power: ${power:,.2f}")

print("\n=== 2. REAL POSITIONS ===")
pos = broker_real.positions(refresh=True)
for _, row in pos.iterrows():
    print(f"  - {row['code']}: {row['qty']} 股/张 | 市值: ${float(row['market_val']):,.2f} | 成本: ${float(row['cost_price']):,.2f} | 盈亏: ${float(row['pl_val']):,.2f}")

print("\n=== 3. REAL TRADING GATES & SAFETY SETTINGS ===")
print(f"FUTU_TRADING_ENV:            {settings.trading_env}")
print(f"REAL_TRADING_ENABLED:        {settings.real_trading_enabled}")
print(f"REAL_ACCOUNT_ID:             {settings.real_account_id}")
print(f"REAL_ALLOWED_STRATEGIES:     {settings.real_allowed_strategies}")
print(f"REAL_MAX_ORDER_NOTIONAL:     ${settings.real_max_order_notional:,.2f}")
print(f"REAL_MAX_DAILY_NOTIONAL:     ${settings.real_max_daily_notional:,.2f}")
print(f"REAL_REQUIRE_MANUAL_CONFIRM: {settings.real_require_manual_confirm}")

print("\n=== 4. RISK & KILL SWITCH STATUS ===")
limits = RiskLimits.load()
kill_switch = store.kill_switch()
print(f"Kill Switch Active:          {kill_switch}")
print(f"Max Daily Loss Limit:        ${limits.max_daily_loss:,.2f}")
print(f"Max Position Weight:         {limits.max_position_weight*100:.1f}%")

print("\n=== 5. OPEND GATEWAY CONNECTION ===")
alive = broker_real.check_connection()
print(f"OpenD Connection Alive:      {alive}")
