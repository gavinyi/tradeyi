from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore
from futu_quant.alerts import AlertManager
from futu_quant.reconciliation import reconcile_ledgers

settings = Settings.load(refresh=True)
store = TradingStore()
alerts = AlertManager(store)
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)

df = reconcile_ledgers(store, broker_real)
print("Reconciliation results:")
for _, r in df.iterrows():
    print(f"  Symbol: {r['symbol']:<10} | Actual: {r['broker_quantity']:<6} | Ledger: {r['ledger_quantity']:<6} | Status: {r['status']:<10} | Strategies: {r['affected_strategies']}")

mismatches = df[df["status"] == "MISMATCH"]
print(f"\nTotal Mismatches: {len(mismatches)}")
assert len(mismatches) == 0, f"Expected 0 mismatches, got {len(mismatches)}"

rec_blocked = store.get_setting("recovery_blocked", "false")
print("store.recovery_blocked:", rec_blocked)
assert rec_blocked == "false"

# 解除报警
alerts.recover("LEDGER_MISMATCH", "三账对账已全面核实恢复：实盘期权无异常偏差，用户原生手工存量底仓安全受控")

nvda_blocked = store.get_setting("symbol_blocked:US.NVDA", "false")
gld_blocked = store.get_setting("symbol_blocked:US.GLD", "false")
print(f"symbol_blocked:US.NVDA = {nvda_blocked}")
print(f"symbol_blocked:US.GLD = {gld_blocked}")
assert nvda_blocked == "false" and gld_blocked == "false"
print("\n>>> RECONCILIATION FIX 100% VERIFIED: ZERO MISMATCHES, ALERTS RECOVERED CLEANLY! <<<")
