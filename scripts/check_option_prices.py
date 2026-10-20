from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
import pandas as pd

settings = Settings.load()
broker = FutuPaperBroker(settings)

# 查询几个活跃标的的最新期权价格
sample_options = [
    "US.AMD261023P550000",
    "US.AMD261023P525000",
    "US.NVDA261021P220000",
    "US.COIN261023P180000",
]

quotes = broker.snapshots(sample_options)
print("Option quotes sample:")
for _, r in quotes.iterrows():
    ask = float(r.get("ask_price", 0) or 0)
    last = float(r.get("last_price", 0) or 0)
    price = ask if ask > 0 else last
    contract_cost = price * 100
    print(f"Code: {r.get('code'):<24} | Price: ${price:.2f} | 1张合约成本: ${contract_cost:,.2f}")
