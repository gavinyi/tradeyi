from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
import pandas as pd

settings = Settings.load()
broker = FutuPaperBroker(settings)

print("Broker trading_env:", broker.settings.trading_env)
pos = broker.positions()
print("Positions in broker:")
if not pos.empty:
    for _, r in pos.iterrows():
        print(f"Code: {r.get('code')} | Qty: {r.get('qty')} | Cost: {r.get('cost_price')}")
else:
    print("EMPTY positions")
