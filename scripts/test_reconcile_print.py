from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore
from futu_quant.reconciliation import reconcile_ledgers

settings = Settings.load(refresh=True)
store = TradingStore()
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)

df = reconcile_ledgers(store, broker_real)
print(f"Total reconciliation rows: {len(df)}")
for _, r in df.iterrows():
    print(f"Symbol: {r['symbol']:<24} | Broker: {r['broker_quantity']:<6} | Ledger: {r['ledger_quantity']:<6} | Status: {r['status']:<10} | Owners: {r['affected_strategies']}")

is_disabled = store.get_setting("strategy_reconciliation_disabled:volatility_long_gamma", "false")
print("\nstrategy_reconciliation_disabled:volatility_long_gamma:", is_disabled)
assert is_disabled.lower() != "true"
print("SUCCESS: Strategy volatility_long_gamma remains healthy and fully enabled!")
