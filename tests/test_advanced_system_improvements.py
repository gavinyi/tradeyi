"""
针对四大进阶系统优化的综合测试套件：
1. 期权到期日 (DTE=0) 兜底强制平仓保护 (Forced Expiry Safe Exit)
2. 纯内置自闭环 ML 打分引擎 (Pure Builtin ML Scorer)
3. 闲置现金国债 ETF (SGOV) 智能理财与动态赎回调度 (Cash Yield Engine)
4. 紧急止盈平仓单网络抖动瞬时保单重试 (Urgent Exit Durable Retry)
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
import pandas as pd

from futu_quant.core_satellite import build_rebalance_plan
from futu_quant.ml_scorer import load_ml_model
from futu_quant.option_lifecycle import execute_disposition_plans
from futu_quant.store import TradingStore
from futu_quant.execution import ExecutionManager


def test_builtin_ml_scorer_pure_python_closed_loop():
    """验证 ML 标的打分器无需 sklearn 也可稳定产出标准化概率，实现完全自闭环。"""
    model, cols = load_ml_model(None)
    assert model is not None
    assert len(cols) > 0
    # 模拟特征输入
    df = pd.DataFrame([{col: 0.5 for col in cols}])
    probs = model.predict_proba(df)
    assert probs.shape == (1, 2)
    assert 0.0 <= probs[0, 1] <= 1.0


def test_forced_expiry_safe_exit_without_virtual_owner():
    """验证当虚拟账本漏记、但真实券商持有到期实值期权时，系统自动安全兜底平仓，防被动交割。"""
    db_path = Path("data/test_expiry_guard.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        # 写入一条到期日当天 (days_to_expiry=0) 的实值期权生命周期记录
        store.record_option_lifecycle([{
            "code": "US.AMD261023C150000",
            "underlying": "US.AMD",
            "option_type": "CALL",
            "strike": 150.0,
            "expiration": "2026-10-23",
            "quantity": 1.0,
            "underlying_price": 165.0,
            "days_to_expiry": 0,
            "intrinsic_value": 15.0,
            "severity": "CRITICAL",
            "action": "立即平仓",
            "reason": "测试到期实值",
            "projected_cash_need": 0.0,
            "projected_share_need": 0.0,
        }])

        mock_broker = MagicMock()
        mock_broker.snapshots.return_value = pd.DataFrame([{"bid_price": 14.50, "last_price": 14.60}])
        mock_executor = MagicMock()
        mock_session = MagicMock()
        mock_session.pre_close.return_value = True

        results = execute_disposition_plans(
            mock_broker, store, mock_executor, mock_session, datetime.now(timezone.utc),
        )
        assert len(results) == 1
        assert mock_executor.submit_limit.called
        call_args = mock_executor.submit_limit.call_args
        assert call_args[0][0] == "volatility_long_gamma"  # 兜底归属
        assert call_args[0][1].startswith("expiry-auto-close:")
        assert call_args[0][3] == "SELL"
    finally:
        db_path.unlink(missing_ok=True)


def test_cash_yield_sgov_allocation_and_liquidity_redemption():
    """验证超额闲置资金自动买入 SGOV 生息，主力股票需资金时优先卖出 SGOV 赎回。"""
    dates = pd.date_range("2026-01-01", periods=260)
    spy_df = pd.DataFrame({"time_key": dates.strftime("%Y-%m-%d"), "close": [500.0 + i * 0.2 for i in range(260)]})
    sgov_df = pd.DataFrame({"time_key": dates.strftime("%Y-%m-%d"), "close": [100.50 for _ in range(260)]})
    frames = {"US.SPY": spy_df, "US.SGOV": sgov_df}

    # 1. 大额闲置资金测试 (总资金 50 万，股票市值 10 万，闲置 40 万，预留缓冲 8%) -> 触发 SGOV 买入理财
    positions = pd.DataFrame([{"code": "US.SPY", "qty": 200.0, "can_sell_qty": 200.0, "market_val": 100000.0}])
    orders, _ = build_rebalance_plan(
        frames=frames,
        positions=positions,
        account_value=500000.0,
        symbols=("US.SPY",),
        cash_buffer=0.08,  # 预留 4 万缓冲
        cash_yield_enabled=True,
        cash_yield_symbol="US.SGOV",
    )
    sgov_buys = [o for o in orders if o.symbol == "US.SGOV" and o.side == "BUY"]
    assert len(sgov_buys) == 1
    assert "闲置资金自动理财" in sgov_buys[0].reason

    # 2. 战时赎回测试 (当前持有大量 SGOV，而现金低于预留缓冲) -> 触发 SGOV 卖出赎回
    positions_with_sgov = pd.DataFrame([
        {"code": "US.SPY", "qty": 800.0, "can_sell_qty": 800.0, "market_val": 400000.0},
        {"code": "US.SGOV", "qty": 800.0, "can_sell_qty": 800.0, "market_val": 80400.0},
    ])
    orders_redeem, _ = build_rebalance_plan(
        frames=frames,
        positions=positions_with_sgov,
        account_value=500000.0,
        symbols=("US.SPY",),
        cash_buffer=0.10,  # 目标预留 5 万缓冲，当前现金仅约 1.96 万
        cash_yield_enabled=True,
        cash_yield_symbol="US.SGOV",
    )
    sgov_sells = [o for o in orders_redeem if o.symbol == "US.SGOV" and o.side == "SELL"]
    assert len(sgov_sells) == 1
    assert "现金理财池赎回" in sgov_sells[0].reason


def test_urgent_exit_retry_on_network_glitch():
    """验证紧急止盈平仓单在遭遇瞬时网络错误时，自动执行带退避的保单重试。"""
    db_path = Path("data/test_retry_exec.db")
    try:
        db_path.unlink(missing_ok=True)
        store = TradingStore(str(db_path))
        store.set_virtual_allocation("volatility_long_gamma", "US.AMD261023P550000", 1.0, 10.0)
        mock_broker = MagicMock()
        mock_broker.settings = MagicMock(trading_env="SIMULATE")
        mock_broker.trd_env = "SIMULATE"
        mock_broker.positions.return_value = pd.DataFrame([{"code": "US.AMD261023P550000", "qty": 1.0, "can_sell_qty": 1.0}])

        # 模拟前 2 次网络断开，第 3 次成功返回
        mock_broker.place_limit_order.side_effect = [
            RuntimeError("positions failed: 网络中断"),
            RuntimeError("Connection timeout"),
            pd.DataFrame([{"order_id": "ORD-RETRY-SUCCESS"}]),
        ]
        executor = ExecutionManager(mock_broker, store)

        # 提交紧急减仓单
        res = executor.submit_limit(
            "volatility_long_gamma",
            "test:urgent:retry:1",
            "US.AMD261023P550000",
            "SELL",
            1,
            22.0,
            {"urgent": True, "reduce_only": True, "reference_price": 22.0},
        )
        assert res.broker_order_id == "ORD-RETRY-SUCCESS"
        assert mock_broker.place_limit_order.call_count == 3
    finally:
        db_path.unlink(missing_ok=True)
