from futu_quant.config import Settings
from futu_quant.store import TradingStore

settings = Settings.load()
store = TradingStore(read_only=True)
hash_val = store.get_setting("admin_password_hash")
print("admin_password_hash in DB:", bool(hash_val))
