import os
import sys
from pathlib import Path

_local_appdata = Path(__file__).resolve().parent.parent / "data" / "appdata"
_local_appdata.mkdir(parents=True, exist_ok=True)
os.environ["APPDATA"] = str(_local_appdata)

from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker
from futu import TrdEnv

settings = Settings.load()
broker = FutuPaperBroker(settings)

with broker.trade_context() as ctx:
    ret, data = ctx.accinfo_query(trd_env=TrdEnv.REAL, acc_id=281756481476169109, currency="USD")
    print("RET:", ret)
    if ret == 0 and not data.empty:
        r = data.iloc[0]
        print(f"Total Assets: ${r.get('total_assets')}")
        print(f"Cash: ${r.get('cash')}")
        print(f"Power: ${r.get('power')}")
    else:
        print("accinfo_query err:", data)
