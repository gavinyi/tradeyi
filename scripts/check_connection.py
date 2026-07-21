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
    main()

