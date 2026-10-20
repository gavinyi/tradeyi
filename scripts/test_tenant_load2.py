import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from futu_quant.config import Settings
from futu_quant.store import TradingStore
from futu_quant.ui import get_registered_tenants
from futu_quant.broker import FutuPaperBroker
import pandas as pd

settings = Settings.load(refresh=True)
store = TradingStore()
tenants = get_registered_tenants(settings)

for tid, t in tenants.items():
    print(f"\n--- Testing Tenant: {tid} ({t['short_name']}) ---")
    b = FutuPaperBroker(settings, trading_env_override=t["env"], account_id_override=t["acc_id"])
    accs = b.accounts()
    info = b.account_info()
    pos = b.positions()
    print(f"Accounts: {len(accs)}, Assets: ${info.iloc[0].get('total_assets') if not info.empty else None}")
    print(f"Positions count: {len(pos)}")
    if not pos.empty:
        for _, r in pos.head(3).iterrows():
            print(f"  - {r.get('code')}: {r.get('qty')} 股, 市值 ${r.get('market_val')}")
