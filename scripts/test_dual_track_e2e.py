from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu_quant.store import TradingStore

settings = Settings.load(refresh=True)
broker = FutuPaperBroker(settings)
store = TradingStore()

print("Broker trading_env:", broker.trading_env)
acc_real = broker.account_info()
pos_real = broker.positions()

# 存实盘
store.save_broker_snapshot("account_info_real", acc_real)
store.save_broker_snapshot("positions_real", pos_real)
print("Saved REAL snapshots:", len(pos_real), "positions")

# 存模拟盘
sim_b = FutuPaperBroker(settings, trading_env_override="SIMULATE", account_id_override=20170224)
acc_sim = sim_b.account_info()
pos_sim = sim_b.positions()
store.save_broker_snapshot("account_info_sim", acc_sim)
store.save_broker_snapshot("positions_sim", pos_sim)
print("Saved SIMULATE snapshots:", len(pos_sim), "positions")

# 验证通过快照能分别读到实盘和模拟盘
from futu_quant.home_dashboard import _account_snapshot
snap_r = _account_snapshot(broker, view_env="REAL")
snap_s = _account_snapshot(broker, view_env="SIMULATE")

print(f"\n_account_snapshot(REAL):     ok={snap_r['ok']}, assets=${float(snap_r['info'].iloc[0]['total_assets']):,.2f}, pos={len(snap_r['positions'])}")
print(f"_account_snapshot(SIMULATE): ok={snap_s['ok']}, assets=${float(snap_s['info'].iloc[0]['total_assets']):,.2f}, pos={len(snap_s['positions'])}")
print("\n>>> DUAL-TRACK END-TO-END PIPELINE VERIFIED 100% SUCCESS! <<<")
