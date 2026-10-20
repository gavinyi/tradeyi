from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker

settings = Settings.load(refresh=True)

# 1. 测试实盘
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)
pos_real = broker_real.positions()
info_real = broker_real.account_info()
print("REAL positions:", len(pos_real), "assets:", info_real.iloc[0].get("total_assets") if not info_real.empty else None)

# 2. 测试美股模拟盘 20170229
broker_sim = FutuPaperBroker(settings, trading_env_override="SIMULATE", account_id_override=20170229)
pos_sim = broker_sim.positions()
info_sim = broker_sim.account_info()
print("SIMULATE positions:", len(pos_sim), "assets:", info_sim.iloc[0].get("total_assets") if not info_sim.empty else None)
