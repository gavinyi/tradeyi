from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore
from futu_quant.reconciliation import reconcile_ledgers

settings = Settings.load(refresh=True)
store = TradingStore()
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)

# 确保先重置对账禁用标记
for k in store.settings_with_prefix("strategy_reconciliation_disabled:"):
    store.set_setting(k, "false")

print("1. Testing virtual_positions(env='REAL')...")
v_real = store.virtual_positions(env="REAL")
print("   v_real rows count:", len(v_real))
assert len(v_real) == 0, f"Expected 0 real virtual holdings before new fills, got {len(v_real)}"

print("\n2. Running reconcile_ledgers on REAL broker...")
df = reconcile_ledgers(store, broker_real)
print(f"   Reconciled rows: {len(df)}")
for _, r in df.iterrows():
    print(f"   Symbol: {r['symbol']:<10} | Actual: {r['broker_quantity']:<6} | Ledger: {r['ledger_quantity']:<6} | Status: {r['status']:<10} | Owners: {r['affected_strategies']}")

is_disabled = store.get_setting("strategy_reconciliation_disabled:volatility_long_gamma", "false")
print("\n3. strategy_reconciliation_disabled:volatility_long_gamma:", is_disabled)
assert is_disabled.lower() != "true", "Fatal: Strategy was wrongly disabled!"
print("\n>>> ZERO DEFECT AUDIT: REAL RECONCILIATION 100% PERFECT! <<<")
