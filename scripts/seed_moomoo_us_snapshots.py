from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore

settings = Settings.load(refresh=True)
store = TradingStore()

# 1. 采集并落库 Moomoo US (283445327916889204)
broker_moomoo = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=283445327916889204)
info_moomoo = broker_moomoo.account_info(refresh=True)
pos_moomoo = broker_moomoo.positions(refresh=True)
orders_moomoo = broker_moomoo.orders()

print("Moomoo US Assets:   $", float(info_moomoo.iloc[0]["total_assets"]))
print("Moomoo US Cash:     $", float(info_moomoo.iloc[0]["cash"]))
print("Moomoo US Positions:", len(pos_moomoo))

store.save_broker_snapshot("account_info_real", info_moomoo)
store.save_broker_snapshot("positions_real", pos_moomoo)
store.save_broker_snapshot("orders_real", orders_moomoo)
if not info_moomoo.empty:
    r = info_moomoo.iloc[0]
    store.record_equity(
        float(r.get("total_assets", 0) or 0),
        float(r.get("cash", 0) or 0),
        float(r.get("market_val", 0) or 0),
        env="REAL",
    )
print("Saved Moomoo US snapshots to database successfully!")

# 2. 采集并落库 美股模拟盘 (20170229)
broker_sim = FutuPaperBroker(settings, trading_env_override="SIMULATE", account_id_override=20170229)
info_sim = broker_sim.account_info(refresh=True)
pos_sim = broker_sim.positions(refresh=True)
orders_sim = broker_sim.orders()

print("\nSimulate Assets:   $", float(info_sim.iloc[0]["total_assets"]))
print("Simulate Positions:", len(pos_sim))

store.save_broker_snapshot("account_info_sim", info_sim)
store.save_broker_snapshot("positions_sim", pos_sim)
store.save_broker_snapshot("orders_sim", orders_sim)
if not info_sim.empty:
    r2 = info_sim.iloc[0]
    store.record_equity(
        float(r2.get("total_assets", 0) or 0),
        float(r2.get("cash", 0) or 0),
        float(r2.get("market_val", 0) or 0),
        env="SIMULATE",
    )
print("Saved Simulate snapshots to database successfully!")
