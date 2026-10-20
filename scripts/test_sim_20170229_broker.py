from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker

settings = Settings.load(refresh=True)

# 1. 模拟盘通道
broker_sim = FutuPaperBroker(settings, trading_env_override="SIMULATE", account_id_override=20170229)
accs_sim = broker_sim.accounts()
info_sim = broker_sim.account_info()
pos_sim = broker_sim.positions()

print(f"broker_sim SUCCESS: account_id={accs_sim.iloc[0]['acc_id']}, assets=${float(info_sim.iloc[0]['total_assets']):,.2f}")
print(f"Positions count: {len(pos_sim)}")
for _, r in pos_sim.head(5).iterrows():
    print(f"  - {r.get('code')}: {r.get('qty')} 股, 市值 ${r.get('market_val')}")

# 2. 实盘通道
broker_real = FutuPaperBroker(settings, trading_env_override="REAL", account_id_override=settings.real_account_id)
accs_real = broker_real.accounts()
info_real = broker_real.account_info()
pos_real = broker_real.positions()

print(f"\nbroker_real SUCCESS: account_id={accs_real.iloc[0]['acc_id']}, assets=${float(info_real.iloc[0]['total_assets']):,.2f}")
print(f"Positions count: {len(pos_real)}")
for _, r in pos_real.iterrows():
    print(f"  - {r.get('code')}: {r.get('qty')} 股, 市值 ${r.get('market_val')}")
