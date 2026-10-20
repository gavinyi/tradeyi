import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from futu_quant.config import Settings
from futu_quant.ui import get_registered_tenants
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore

settings = Settings.load(refresh=True)
store = TradingStore()
tenants = get_registered_tenants(settings)

for tid, t in tenants.items():
    print(f"Ingesting snapshot for {tid} ({t['short_name']})...")
    b = FutuPaperBroker(
        settings,
        trading_env_override=t["env"],
        account_id_override=t["acc_id"],
        security_firm_override=t.get("security_firm"),
    )
    accs = b.accounts()
    info = b.account_info(refresh=True)
    pos = b.positions(refresh=True)
    orders = b.orders()
    
    # 租户独立专属快照
    store.save_broker_snapshot(f"accounts_{tid}", accs)
    store.save_broker_snapshot(f"account_info_{tid}", info)
    store.save_broker_snapshot(f"positions_{tid}", pos)
    store.save_broker_snapshot(f"orders_{tid}", orders)
    print(f"  -> {tid}: Assets=${info.iloc[0].get('total_assets') if not info.empty else None}, Pos={len(pos)}")

# 兼容默认 key
store.save_broker_snapshot("account_info_real", store.broker_snapshot("account_info_moomoo_real")["payload"])
store.save_broker_snapshot("positions_real", store.broker_snapshot("positions_moomoo_real")["payload"])
store.save_broker_snapshot("account_info_sim", store.broker_snapshot("account_info_sim_primary")["payload"])
store.save_broker_snapshot("positions_sim", store.broker_snapshot("positions_sim_primary")["payload"])

print("\n>>> ALL 3 TENANT SNAPSHOTS SAFELY INGESTED! <<<")
