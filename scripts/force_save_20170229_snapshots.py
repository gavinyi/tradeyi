from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore

settings = Settings.load(refresh=True)
store = TradingStore()

# 1. 采集并保存美股模拟盘 20170229 真实快照
broker_sim = FutuPaperBroker(settings, trading_env_override="SIMULATE", account_id_override=20170229)
info_sim = broker_sim.account_info(refresh=True)
pos_sim = broker_sim.positions(refresh=True)
orders_sim = broker_sim.orders()

print(f"Queried 20170229: Assets=${float(info_sim.iloc[0]['total_assets']):,.2f}, Positions count={len(pos_sim)}")
store.save_broker_snapshot("account_info_sim", info_sim)
store.save_broker_snapshot("positions_sim", pos_sim)
store.save_broker_snapshot("orders_sim", orders_sim)
if not info_sim.empty:
    r = info_sim.iloc[0]
    store.record_equity(
        float(r.get("total_assets", 0) or 0),
        float(r.get("cash", 0) or 0),
        float(r.get("market_val", 0) or 0),
        env="SIMULATE",
    )
print("Saved 20170229 snapshots into DB successfully!")

# 2. 采集并保存实盘 281756481476169109 真实快照
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)
info_real = broker_real.account_info(refresh=True)
pos_real = broker_real.positions(refresh=True)
orders_real = broker_real.orders()

print(f"Queried REAL: Assets=${float(info_real.iloc[0]['total_assets']):,.2f}, Positions count={len(pos_real)}")
store.save_broker_snapshot("account_info_real", info_real)
store.save_broker_snapshot("positions_real", pos_real)
store.save_broker_snapshot("orders_real", orders_real)
if not info_real.empty:
    r2 = info_real.iloc[0]
    store.record_equity(
        float(r2.get("total_assets", 0) or 0),
        float(r2.get("cash", 0) or 0),
        float(r2.get("market_val", r2.get("securities_assets", 0)) or 0),
        env="REAL",
    )
print("Saved REAL snapshots into DB successfully!")
