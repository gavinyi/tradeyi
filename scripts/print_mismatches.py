from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore
from futu_quant.reconciliation import reconcile_ledgers

settings = Settings.load(refresh=True)
store = TradingStore()
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)

df = reconcile_ledgers(store, broker_real)
mismatches = df[df["status"] == "MISMATCH"]
print(f"Mismatches ({len(mismatches)}):")
for _, r in mismatches.iterrows():
    print(dict(r))
