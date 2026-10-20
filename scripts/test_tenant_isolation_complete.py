import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from futu_quant.config import Settings
from futu_quant.store import TradingStore
from futu_quant.risk import RiskEngine
from futu_quant.reconciliation import reconcile_ledgers


class MockBroker:
    def __init__(self, account_id, env="REAL", positions_df=None):
        self.account_id = account_id
        self.trading_env = env
        self.trd_env = "REAL" if env == "REAL" else "SIMULATE"
        self._positions = positions_df if positions_df is not None else pd.DataFrame()
        self.settings = Settings.load(refresh=True)

    def positions(self, refresh=False):
        return self._positions

    def orders(self, historical=False):
        return pd.DataFrame()

    def is_cancellable_status(self, status):
        return False


def test_complete_isolation():
    print("=== 全面多租户与账户隔离深度验证 ===")
    store = TradingStore()

    MOOMOO_ID = "283445327916889204"
    FUTUHK_ID = "281756481476169109"
    SIM_ID = "20170229"

    # --- 1. 急停隔离验证 ---
    print("\n--- [1] 急停状态多租户单账户物理隔离 ---")
    # 先清理当前状态
    store.set_kill_switch(False)
    store.set_kill_switch(False, account_id=MOOMOO_ID)
    store.set_kill_switch(False, account_id=FUTUHK_ID)
    store.set_kill_switch(False, account_id=SIM_ID)

    # 仅开启 Moomoo US 急停
    store.set_kill_switch(True, account_id=MOOMOO_ID)
    assert store.kill_switch(account_id=MOOMOO_ID) is True, "Moomoo 急停必须为 True"
    assert store.kill_switch(account_id=FUTUHK_ID) is False, "富途香港 急停必须依然为 False"
    assert store.kill_switch(account_id=SIM_ID) is False, "模拟盘 急停必须依然为 False"
    print("  [PASS] 1.1 触发 Moomoo 急停时，富途香港与模拟盘状态均为 False，互不影响")

    # 风控引擎拦截隔离测试
    risk = RiskEngine(store)
    b_moomoo = MockBroker(MOOMOO_ID, env="REAL")
    b_futuhk = MockBroker(FUTUHK_ID, env="REAL")

    dec_moomoo = risk.check(b_moomoo, "US.SPY260618C00500000", "BUY", 1, 5.0, 100, strategy_id="volatility_long_gamma", execution_mode="REAL")
    assert not dec_moomoo.allowed, "Moomoo 买单应当被单账户急停拦截"
    assert f"账户 {MOOMOO_ID} 急停已开启" in dec_moomoo.reason
    print("  [PASS] 1.2 Moomoo 账户下单被精准拦截")

    dec_futuhk = risk.check(b_futuhk, "US.SPY", "BUY", 1, 500.0, 1, strategy_id="core_satellite", execution_mode="REAL")
    assert dec_futuhk.allowed or "急停" not in dec_futuhk.reason, f"富途香港不应受 Moomoo 急停影响: {dec_futuhk.reason}"
    print("  [PASS] 1.3 富途香港 账户未受任何急停影响，正常放行")

    # 解除 Moomoo 急停
    store.set_kill_switch(False, account_id=MOOMOO_ID)
    assert store.kill_switch(account_id=MOOMOO_ID) is False
    print("  [PASS] 1.4 单账户急停解除成功")

    # 全局总制动测试
    store.set_kill_switch(True) # 全局总制动
    assert store.kill_switch(account_id=MOOMOO_ID) is True
    assert store.kill_switch(account_id=FUTUHK_ID) is True
    assert store.kill_switch(account_id=SIM_ID) is True
    print("  [PASS] 1.5 全局总制动生效时，所有账户均受控停止")
    store.set_kill_switch(False) # 恢复

    # --- 2. 三账对账隔离验证 ---
    print("\n--- [2] 三账对账多账户隔离验证 ---")
    hk_pos = pd.DataFrame([
        {"code": "US.NVDA", "qty": 8.0},
        {"code": "US.GLD", "qty": 4.4344},
    ])
    b_hk_with_pos = MockBroker(FUTUHK_ID, env="REAL", positions_df=hk_pos)
    b_moomoo_empty = MockBroker(MOOMOO_ID, env="REAL", positions_df=pd.DataFrame())

    rep_moomoo = reconcile_ledgers(store, b_moomoo_empty)
    assert rep_moomoo.empty, f"Moomoo 0持仓对账结果必须为空 DataFrame，实际为 {rep_moomoo}"
    assert "status" in rep_moomoo.columns, "必须包含 status 列"
    print("  [PASS] 2.1 Moomoo 空仓账户对账完全干净，无任何外部标的污染")

    rep_hk = reconcile_ledgers(store, b_hk_with_pos)
    assert not rep_hk.empty, "富途香港对账必须包含 NVDA/GLD"
    assert set(rep_hk["symbol"]) == {"US.NVDA", "US.GLD"}
    assert all(rep_hk["status"] == "MATCHED"), "存量底仓必须匹配为 MATCHED"
    print("  [PASS] 2.2 富途香港对账精确包含且仅包含 NVDA/GLD，判定为安全受控底仓")

    # --- 3. 租户快照与数据隔离验证 ---
    print("\n--- [3] 租户快照与数据物理隔离验证 ---")
    snap_moomoo = store.broker_snapshot("account_info", tenant_id="moomoo_real")
    snap_futuhk = store.broker_snapshot("account_info", tenant_id="futuhk_real")
    snap_sim = store.broker_snapshot("account_info", tenant_id="sim_primary")

    assert snap_moomoo is not None and "moomoo" in snap_moomoo["key"]
    assert snap_futuhk is not None and "futuhk" in snap_futuhk["key"]
    assert snap_sim is not None and "sim" in snap_sim["key"]
    print("  [PASS] 3.1 租户专属快照 key 读取互不串联")

    print("\n================ 全部多租户隔离验证通过！================")


if __name__ == "__main__":
    test_complete_isolation()
