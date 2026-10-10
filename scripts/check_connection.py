import os
from pathlib import Path
import sys

if sys.platform == "win32":
    _local_appdata = Path(__file__).resolve().parents[1] / "data" / "appdata"
    _local_appdata.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(_local_appdata)

from futu_quant.broker import FutuPaperBroker
from futu_quant.config import Settings


def main() -> None:
    broker = FutuPaperBroker(Settings.load())
    accounts = broker.accounts()
    info = broker.account_info()
    print(f"OK: connected to {len(accounts)} SIMULATE account(s)")
    columns = [name for name in ["acc_id", "trd_env", "acc_type", "uni_card_num"] if name in accounts]
    print(accounts[columns].to_string(index=False))
    if not info.empty:
        print(f"total_assets={info.iloc[0].get('total_assets')}")


if __name__ == "__main__":
    try:
        main()
    finally:
        from futu_quant.broker import close_all_connections
        close_all_connections()

