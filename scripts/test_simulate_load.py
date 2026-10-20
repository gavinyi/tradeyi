from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker

settings = Settings.load(refresh=True)
target_b = FutuPaperBroker(settings, trading_env_override="SIMULATE", account_id_override=20170229)

print("Broker created. Testing load_account equivalents:")
try:
    accs = target_b.accounts()
    print("accs:", len(accs))
    info = target_b.account_info()
    print("info:", len(info), "assets:", info.iloc[0].get("total_assets") if not info.empty else None)
    pos = target_b.positions()
    print("pos:", len(pos))
    if not pos.empty:
        print("pos codes:", pos["code"].tolist()[:5])
    orders = target_b.orders()
    print("orders:", len(orders))
except Exception as e:
    import traceback
    traceback.print_exc()
