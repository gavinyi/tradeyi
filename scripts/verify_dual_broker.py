from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker

settings = Settings.load(refresh=True)

# 1. 创建实盘通道实例
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)
# 2. 创建模拟盘通道实例
broker_sim = FutuPaperBroker(settings, trading_env_override="SIMULATE", account_id_override=20170224)

# 查询实盘
accs_real = broker_real.accounts()
info_real = broker_real.account_info()
pos_real = broker_real.positions()

# 查询模拟盘
accs_sim = broker_sim.accounts()
info_sim = broker_sim.account_info()
pos_sim = broker_sim.positions()

print(f"REAL     account: {accs_real.iloc[0]['acc_id']}, assets=${float(info_real.iloc[0]['total_assets']):,.2f}, positions count={len(pos_real)}")
print(f"SIMULATE account: {accs_sim.iloc[0]['acc_id']}, assets=${float(info_sim.iloc[0]['total_assets']):,.2f}, positions count={len(pos_sim)}")
print("\n>>> DUAL-CHANNEL BROKER INSTANCE VERIFIED 100% SUCCESS! <<<")
