import os
import sys
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from futu_quant.config import Settings
from futu_quant.store import TradingStore
from futu_quant.real_trading import check_real_trade, session_status, enable_session, confirmation_code
from futu_quant.execution import ExecutionManager, ExecutionResult
from futu_quant.core_satellite import build_rebalance_plan


class MockBroker:
    def __init__(self, settings, env="REAL", account_id=281756481476169109, firm="FUTUSECURITIES", positions_df=None):
        self.settings = settings
        self.trading_env = env
        self.trd_env = "REAL" if env == "REAL" else "SIMULATE"
        self.account_id = account_id
        self.security_firm = type("Firm", (), {"name": firm})()
        self._positions = positions_df if positions_df is not None else pd.DataFrame([
            {"code": "US.NVDA", "qty": 8.0, "can_sell_qty": 8.0},
            {"code": "US.GLD", "qty": 4.4344, "can_sell_qty": 4.4344},
        ])

    def positions(self, refresh=False):
        return self._positions

    def orders(self, historical=False):
        return pd.DataFrame()

    def is_cancellable_status(self, status):
        return False

    def place_limit_order(self, symbol, side, quantity, price, **kwargs):
        return pd.DataFrame([{"order_id": "MOCK_ORDER_123"}])


def test_suite():
    print("=== 开始多账户隔离与底仓调仓保护验证 ===")
    
    # 建立测试用隔离临时存储
    db_file = Path("data/test_isolation_temp.db")
    if db_file.exists():
        try:
            db_file.unlink()
        except Exception:
            pass
    store = TradingStore(db_file)
    
    # 模拟全局实盘配置
    from dataclasses import replace
    base = Settings.load(refresh=True)
    settings = replace(
        base,
        trading_env="REAL",
        security_firm="FUTUINC",
        real_account_id=283445327916889204,
        real_trading_enabled=True,
        real_allowed_strategies=("core_satellite", "volatility_long_gamma"),
        real_max_order_notional=2000.0,
        real_max_daily_notional=5000.0,
        real_require_manual_confirm=False,
    )
    
    # --- 测试 1: 多账户策略与券商实体严格隔离 ---
    print("\n--- [测试 1] 多账户策略与券商实体严格隔离 ---")
    
    # 1.1 Moomoo US 运行 volatility_long_gamma: 应允许
    dec1 = check_real_trade(
        store, settings, "volatility_long_gamma", "US.SPY260618C00500000", "BUY", 1, 5.0,
        metadata={"account_id": "283445327916889204", "security_firm": "FUTUINC", "multiplier": 100},
    )
    assert dec1.allowed, f"Moomoo 期权应当允许: {dec1.reason}"
    print("  [PASS] 1.1 Moomoo US 实盘账户允许 volatility_long_gamma")
    
    # 1.2 Moomoo US 运行 core_satellite: 必须坚决拒绝
    dec2 = check_real_trade(
        store, settings, "core_satellite", "US.SPY", "BUY", 1, 400.0,
        metadata={"account_id": "283445327916889204", "security_firm": "FUTUINC"},
    )
    assert not dec2.allowed, "Moomoo 账户禁止运行 core_satellite"
    assert "禁止在实盘账户 283445327916889204 执行" in dec2.reason
    print("  [PASS] 1.2 Moomoo US 账户拦截 core_satellite 股票策略")
    
    # 1.3 富途香港 运行 core_satellite: 应允许
    dec3 = check_real_trade(
        store, settings, "core_satellite", "US.SPY", "BUY", 2, 400.0,
        metadata={"account_id": "281756481476169109", "security_firm": "FUTUSECURITIES"},
    )
    assert dec3.allowed, f"富途香港应当允许 core_satellite: {dec3.reason}"
    print("  [PASS] 1.3 富途香港 实盘账户允许 core_satellite")
    
    # 1.4 富途香港 运行 volatility_long_gamma: 必须坚决拒绝
    dec4 = check_real_trade(
        store, settings, "volatility_long_gamma", "US.SPY260618C00500000", "BUY", 1, 5.0,
        metadata={"account_id": "281756481476169109", "security_firm": "FUTUSECURITIES", "multiplier": 100},
    )
    assert not dec4.allowed, "富途香港禁止运行 volatility_long_gamma"
    assert "禁止在实盘账户 281756481476169109 执行" in dec4.reason
    print("  [PASS] 1.4 富途香港 账户拦截 volatility_long_gamma 期权策略")
    
    # 1.5 券商实体错配拦截: Moomoo 账户误传 FUTUSECURITIES
    dec5 = check_real_trade(
        store, settings, "volatility_long_gamma", "US.SPY260618C00500000", "BUY", 1, 5.0,
        metadata={"account_id": "283445327916889204", "security_firm": "FUTUSECURITIES", "multiplier": 100},
    )
    assert not dec5.allowed
    assert "券商实体校验失败" in dec5.reason
    print("  [PASS] 1.5 券商实体错配拦截成功")

    # --- 测试 2: 独立账户配置与日额度隔离 ---
    print("\n--- [测试 2] 独立账户额度隔离与独立开关 ---")
    
    # 富途香港设置上限为 600
    store.set_setting("real_account_config:281756481476169109", '{"enabled": true, "max_order_notional": 600.0, "max_daily_notional": 1200.0}')
    
    dec_limit_fail = check_real_trade(
        store, settings, "core_satellite", "US.SPY", "BUY", 2, 400.0,
        metadata={"account_id": "281756481476169109", "security_firm": "FUTUSECURITIES"},
    )
    assert not dec_limit_fail.allowed
    assert "超过上限 $600.00" in dec_limit_fail.reason
    print("  [PASS] 2.1 独立账户单笔额度 ($600) 正确生效拦截 ($800)")
    
    # 富途香港独立开关关闭
    store.set_setting("real_account_config:281756481476169109", '{"enabled": false}')
    dec_disabled = check_real_trade(
        store, settings, "core_satellite", "US.SPY", "BUY", 1, 100.0,
        metadata={"account_id": "281756481476169109", "security_firm": "FUTUSECURITIES"},
    )
    assert not dec_disabled.allowed
    assert "独立交易开关已关闭" in dec_disabled.reason
    print("  [PASS] 2.2 独立账户关闭时精准拦截该账户")
    
    # 恢复富途香港配置
    store.set_setting("real_account_config:281756481476169109", '{"enabled": true, "max_order_notional": 2000.0, "max_daily_notional": 5000.0}')

    # --- 测试 3: NVDA 5 股底仓物理防卖与 GLD 调仓释放 ---
    print("\n--- [测试 3] NVDA 5股长线底仓防卖与 GLD 调仓释放 ---")
    
    # 模拟富途香港 Broker (持有 8 NVDA, 4.4344 GLD)
    hk_broker = MockBroker(settings, env="REAL", account_id=281756481476169109, firm="FUTUSECURITIES")
    exec_mgr = ExecutionManager(hk_broker, store)
    
    # 3.1 NVDA: 卖出 4 股 (剩余 8 - 4 = 4 < 5): 必须坚决拒绝
    res_nvda_4 = exec_mgr.submit_limit("core_satellite", "test_nvda_4", "US.NVDA", "SELL", 4, 216.0, execution_mode_override="REAL")
    assert res_nvda_4.status == "REJECTED", f"卖出 4 股应被拒绝: {res_nvda_4.message}"
    assert "最低保护底线 5 股" in res_nvda_4.message
    print("  [PASS] 3.1 卖出 4 股 NVDA 导致剩余低于 5 股，被物理拦截")
    
    # 3.2 NVDA: 卖出 3 股 (剩余 8 - 3 = 5 >= 5): 严格允许
    res_nvda_3 = exec_mgr.submit_limit("core_satellite", "test_nvda_3", "US.NVDA", "SELL", 3, 216.0, execution_mode_override="REAL")
    assert res_nvda_3.status in {"SUBMITTED", "FILLED", "UNKNOWN"}, f"卖出 3 股应允许: {res_nvda_3.message}"
    print("  [PASS] 3.2 卖出 3 股 NVDA 刚好保留 5 股底仓，安全通过")
    
    # 3.3 GLD: core_satellite 卖出 4 股: 严格允许
    res_gld = exec_mgr.submit_limit("core_satellite", "test_gld_sell", "US.GLD", "SELL", 4, 381.0, execution_mode_override="REAL")
    assert res_gld.status in {"SUBMITTED", "FILLED", "UNKNOWN"}, f"卖出 4 股 GLD 应允许: {res_gld.message}"
    print("  [PASS] 3.3 core_satellite 卖出 4 股 GLD 调仓释放资金，安全通过")
    
    # 3.4 GLD: 非 core_satellite 策略 (如期权策略) 试图卖出股票: 必须坚决拒绝
    res_gld_unauth = exec_mgr.submit_limit("volatility_long_gamma", "test_gld_unauth", "US.GLD", "SELL", 1, 381.0, execution_mode_override="REAL")
    assert res_gld_unauth.status == "REJECTED"
    assert "无权向实盘提交正股卖单" in res_gld_unauth.message
    print("  [PASS] 3.4 拦截非 core_satellite 策略卖出 GLD")

    # 3.5 Moomoo 账户禁止提交任何正股卖单
    moomoo_broker = MockBroker(settings, env="REAL", account_id=283445327916889204, firm="FUTUINC")
    moomoo_exec_mgr = ExecutionManager(moomoo_broker, store)
    res_moomoo_stock = moomoo_exec_mgr.submit_limit("core_satellite", "test_moomoo_stock", "US.NVDA", "SELL", 1, 216.0, execution_mode_override="REAL")
    assert res_moomoo_stock.status == "REJECTED"
    assert "正股卖单仅允许在富途香港底仓专户" in res_moomoo_stock.message
    print("  [PASS] 3.5 Moomoo 账户禁止提交正股卖单")

    # --- 测试 4: 调仓规划生成验证 ---
    print("\n--- [测试 4] build_rebalance_plan 调仓规划生成验证 ---")
    def make_bars(price, length=220):
        dates = pd.date_range("2026-01-01", periods=length, freq="B")
        return pd.DataFrame({
            "time_key": dates.strftime("%Y-%m-%d %H:%M:%S"),
            "open": [price] * length,
            "high": [price * 1.01] * length,
            "low": [price * 0.99] * length,
            "close": [price] * length,
            "volume": [100000] * length,
        })

    frames = {
        "US.SPY": make_bars(500.0),
        "US.NVDA": make_bars(216.0),
        "US.GLD": make_bars(381.0),
        "US.QQQ": make_bars(450.0),
    }
    pos_df = pd.DataFrame([
        {"code": "US.NVDA", "qty": 8.0, "can_sell_qty": 8.0, "market_val": 1728.0},
        {"code": "US.GLD", "qty": 4.4344, "can_sell_qty": 4.4344, "market_val": 1690.0},
    ])
    account_value = 3426.0 # USD account value
    
    orders, risk_on = build_rebalance_plan(
        frames=frames,
        positions=pos_df,
        account_value=account_value,
        symbols=("US.SPY", "US.NVDA", "US.GLD", "US.QQQ"),
        max_stock_weight=0.70,
        min_trade_usd=100.0,
        profile="balanced",
        weight_tolerance=0.01,
        max_single_rebalance_pct=1.0,
    )
    
    print(f"  生成调仓订单数量: {len(orders)}")
    for o in orders:
        print(f"    - 订单: {o.side} {o.quantity}股 {o.symbol} @ ${o.price:.2f} ({o.reason})")
    
    # 验证 GLD 调仓卖单生成 (卖出 4 股)
    gld_orders = [o for o in orders if o.symbol == "US.GLD"]
    assert len(gld_orders) == 1, "必须生成 GLD 调仓单"
    assert gld_orders[0].side == "SELL", "GLD 必须为 SELL"
    assert gld_orders[0].quantity == 4, f"GLD 卖出数量应为 4，实际为 {gld_orders[0].quantity}"
    print("  [PASS] 4.1 GLD 正确生成清仓调仓卖单 (4 股)")
    
    # 4.3 显式降低仓位上限，迫使 NVDA 触发顶格减仓，验证是否精准卖出 3 股并保留 5 股
    orders_low_cap, _ = build_rebalance_plan(
        frames=frames,
        positions=pos_df,
        account_value=account_value,
        symbols=("US.SPY", "US.NVDA", "US.GLD", "US.QQQ"),
        max_stock_weight=0.35, # 极低仓位上限迫使所有持仓缩减至最低保护线
        min_trade_usd=50.0,
        profile="conservative",
        weight_tolerance=0.01,
        max_single_rebalance_pct=1.0,
        max_target_change=0.30,
    )
    nvda_sell_orders = [o for o in orders_low_cap if o.symbol == "US.NVDA" and o.side == "SELL"]
    assert len(nvda_sell_orders) == 1, "必须触发 NVDA 调仓卖单"
    assert nvda_sell_orders[0].quantity == 3, f"NVDA 减仓数量必须精确为 3 股 (8 - 5)，实际为 {nvda_sell_orders[0].quantity}"
    print("  [PASS] 4.3 极度防御下 NVDA 触发顶格调仓，严格卖出 3 股并精确保留 5 股底仓")
        
    print("\n================ 全部 12 项风控与调仓测试全部通过！================")
    
    # 清理测试数据库
    if db_file.exists():
        try:
            db_file.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    test_suite()
