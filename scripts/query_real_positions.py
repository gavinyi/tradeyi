import os
import sys
from pathlib import Path

_local_appdata = Path(__file__).resolve().parent.parent / "data" / "appdata"
_local_appdata.mkdir(parents=True, exist_ok=True)
os.environ["APPDATA"] = str(_local_appdata)

from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu import TrdEnv
import pandas as pd

settings = Settings.load()
broker = FutuPaperBroker(settings)

with broker.trade_context() as ctx:
    ret, data = ctx.position_list_query(trd_env=TrdEnv.REAL, acc_id=281756481476169109)
    print("RET:", ret)
    if ret == 0 and not data.empty:
        print(f"Total real positions: {len(data)}")
        for _, r in data.iterrows():
            print(f"Code: {r.get('code'):<15} | Name: {r.get('stock_name'):<12} | Qty: {r.get('qty')} | CanSell: {r.get('can_sell_qty')} | Cost: {r.get('cost_price')} | NominalPrice: {r.get('nominal_price')} | MarketVal: ${r.get('market_val')} | PnL: ${r.get('pl_val')} ({r.get('pl_ratio')}%)")
    else:
        print("position_list_query data:", data)
