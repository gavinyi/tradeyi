from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore

settings = Settings.load(refresh=True)
store = TradingStore()

# 连接真正的美股模拟盘 20170229
broker_sim = FutuPaperBroker(settings, trading_env_override="SIMULATE", account_id_override=20170229)
info_sim = broker_sim.account_info()
pos_sim = broker_sim.positions()
orders_sim = broker_sim.orders()

print(f"Queried 20170229: Assets=${info_sim.iloc[0]['total_assets']}, Positions count={len(pos_sim)}")

# 存入数据库的 positions_sim 和 account_info_sim
store.save_broker_snapshot("account_info_sim", info_sim)
store.save_broker_snapshot("positions_sim", pos_sim)
store.save_broker_snapshot("orders_sim", orders_sim)
print("Saved 20170229 snapshots into database successfully!")

# 连接真实的实盘账户 281756481476169109
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)
info_real = broker_real.account_info()
pos_real = broker_real.positions()
orders_real = broker_real.orders()

print(f"Queried real: Assets=${info_real.iloc[0]['total_assets']}, Positions count={len(pos_real)}")
store.save_broker_snapshot("account_info_real", info_real)
store.save_broker_snapshot("positions_real", pos_real)
store.save_broker_snapshot("orders_real", orders_real)
print("Saved REAL snapshots into database successfully!")
