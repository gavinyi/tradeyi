import os
import sys
from pathlib import Path

_local_appdata = Path(__file__).resolve().parent.parent / "data" / "appdata"
_local_appdata.mkdir(parents=True, exist_ok=True)
os.environ["APPDATA"] = str(_local_appdata)

from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker

settings = Settings.load()
broker = FutuPaperBroker(settings)

with broker.trade_context() as ctx:
    ret, data = ctx.get_acc_list()
    print("RET:", ret)
    print("ALL ACCOUNTS COLUMNS:", data.columns.tolist() if hasattr(data, "columns") else "None")
    if hasattr(data, "iterrows"):
        for _, r in data.iterrows():
            print(f"acc_id: {r.get('acc_id')} | trd_env: {r.get('trd_env')} | card_num: {r.get('card_num')} | uni_card_num: {r.get('uni_card_num')}")
