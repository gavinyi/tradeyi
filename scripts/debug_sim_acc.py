from futu_quant.config import Settings
from futu_quant.broker import FutuPaperBroker

settings = Settings.load(refresh=True)
broker_sim = FutuPaperBroker(settings, trading_env_override="SIMULATE", account_id_override=20170224)

with broker_sim.trade_context() as ctx:
    ret, data = ctx.get_acc_list()
    print("ret:", ret)
    print("data:\n", data)
