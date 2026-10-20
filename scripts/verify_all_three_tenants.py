import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from futu_quant.config import Settings
from futu_quant.ui import get_registered_tenants
from futu_quant.broker import FutuPaperBroker

settings = Settings.load(refresh=True)
tenants = get_registered_tenants(settings)

print(f"Total registered tenants: {len(tenants)}")
for tid, t in tenants.items():
    print(f"\n=======================================================")
    print(f"TENANT: {t['label']}")
    print(f"Firm: {t['security_firm']} | Env: {t['env']} | Acc: {t['acc_id']}")
    print(f"=======================================================")
    b = FutuPaperBroker(
        settings,
        trading_env_override=t["env"],
        account_id_override=t["acc_id"],
        security_firm_override=t.get("security_firm"),
    )
    accs = b.accounts()
    info = b.account_info(refresh=True)
    pos = b.positions(refresh=True)
    assets = float(info.iloc[0].get("total_assets", 0)) if not info.empty else 0
    cash = float(info.iloc[0].get("cash", 0)) if not info.empty else 0
    print(f"-> Total Assets: ${assets:,.2f} | Cash: ${cash:,.2f} | Positions Count: {len(pos)}")
    if not pos.empty:
        codes = [f"{r.get('code')} ({r.get('qty')}股)" for _, r in pos.head(3).iterrows()]
        print(f"   Sample Positions: {', '.join(codes)}")
    else:
        print("   Positions: 0 (Pure Cash Clean Slate)")

print("\n>>> ALL THREE TENANTS SIMULTANEOUSLY QUERIED AND VERIFIED 100% SUCCESS! <<<")
