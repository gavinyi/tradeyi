from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker

settings = Settings.load(refresh=True)
broker = FutuPaperBroker(settings)

print("Broker Environment:", broker.settings.trading_env)
print("Querying broker.accounts()...")
accs = broker.accounts()
print(f"Matched accounts count: {len(accs)}")
if not accs.empty:
    r = accs.iloc[0]
    print(f"Locked Real Account: acc_id={r.get('acc_id')}, trd_env={r.get('trd_env')}, uni_card_num={r.get('uni_card_num')}")

print("\nQuerying broker.account_info()...")
info = broker.account_info()
if not info.empty:
    i = info.iloc[0]
    print(f"Real Total Assets: ${float(i.get('total_assets', 0)):,.2f}")
    print(f"Real Cash:         ${float(i.get('cash', 0)):,.2f}")
    print(f"Real Market Value: ${float(i.get('market_val', 0)):,.2f}")

print("\nQuerying broker.positions()...")
pos = broker.positions()
print(f"Real Positions count: {len(pos)}")
for _, p in pos.iterrows():
    print(f"  - {p.get('code')} ({p.get('stock_name')}): {p.get('qty')} 股, 市值 ${p.get('market_val')}")
