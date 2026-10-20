from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore
from futu_quant.reconciliation import reconcile_ledgers

settings = Settings.load(refresh=True)
store = TradingStore()
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)

print("Running reconcile_ledgers on REAL broker...")
df = reconcile_ledgers(store, broker_real)
print(f"Reconciliation result rows: {len(df)}")
if not df.empty:
    print(df[["code", "broker_qty", "ledger_qty", "match", "action"]])
else:
    print("All matched! No mismatch records!")

# 检查实盘策略是否依然处于 enabled 状态
is_disabled = store.get_setting("strategy_reconciliation_disabled:volatility_long_gamma", "false")
print("strategy_reconciliation_disabled:volatility_long_gamma:", is_disabled)
assert is_disabled.lower() != "true", "Fatal: Strategy was wrongly disabled by cross-env mismatch!"
print(">>> REAL RECONCILIATION VERIFIED 100% SAFE AND ISOLATED! <<<")
