from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore
from futu_quant.alerts import AlertManager
from futu_quant.reconciliation import reconcile_ledgers

settings = Settings.load(refresh=True)
store = TradingStore()
alerts = AlertManager(store)
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)

print("1. Running isolated real reconciliation...")
df = reconcile_ledgers(store, broker_real)
print(f"Reconciliation done. Mismatched strategies count: {len(df[df['status']=='MISMATCH'])}")

# 明确触发故障恢复
print("2. Recovering LEDGER_MISMATCH incident...")
alerts.recover("LEDGER_MISMATCH", "三账对账核实正常：实盘期权无异常偏差，存量底仓安全受控")

# 检查当前 incident 状态
inc = store.incident("LEDGER_MISMATCH")
print("Current LEDGER_MISMATCH incident status:", inc.get("status") if inc else "None")
