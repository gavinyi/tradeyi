"""ML标的评分模块"""

from __future__ import annotations

import pickle
import re
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from futu_quant.store import TradingStore

logger = logging.getLogger(__name__)


class BuiltinRuleModel:
    """内置轻量纯 Python 打分器，无需 sklearn，完全通过核心动量、胜率与波动率特征进行标准化打分。"""
    def predict_proba(self, X: pd.DataFrame):
        import numpy as np
        probs = []
        for _, row in X.iterrows():
            win_rate = float(row.get("symbol_win_rate", 0.50))
            pnl_trend = float(row.get("symbol_pnl_trend", 0.0))
            avg_pnl = float(row.get("symbol_avg_pnl", 0.0))
            score = 0.50 + (win_rate - 0.50) * 0.35 + (0.05 if pnl_trend > 0 else (-0.05 if pnl_trend < 0 else 0))
            if avg_pnl > 0:
                score += 0.05
            prob = max(0.05, min(0.95, score))
            probs.append([1.0 - prob, prob])
        return np.array(probs)


def load_ml_model(model_path: str | None = None):
    """
    加载最新的ML模型；无外部 sklearn 依赖时自动回退为纯内置打分器
    """
    default_cols = [
        'days_to_expiry', 'is_call', 'strike_price', 'expiry_bucket',
        'symbol_avg_pnl', 'symbol_std_pnl', 'symbol_max_pnl', 'symbol_min_pnl',
        'symbol_win_rate', 'symbol_3d_avg_pnl', 'symbol_7d_avg_pnl',
        'symbol_trade_count', 'symbol_pnl_trend', 'day_of_week',
        'hour_of_day', 'is_market_close', 'vix_level', 'spy_return_1d',
        'spy_return_5d', 'market_regime'
    ]
    if model_path is None:
        model_path = Path('data/ml_models/volatility_scorer_v3.pkl')
    else:
        model_path = Path(model_path)
    
    if model_path.exists():
        try:
            with open(model_path, 'rb') as f:
                model_data = pickle.load(f)
            logger.info(f"✅ 加载ML模型: {model_path}")
            return model_data['model'], model_data['feature_cols']
        except Exception:
            logger.info("ℹ️  启用内置轻量纯 Python 标的评分引擎 (自闭环无外部依赖)")
            return BuiltinRuleModel(), default_cols

    return BuiltinRuleModel(), default_cols


def parse_option_symbol(symbol: str) -> dict | None:
    """
    解析期权代码
    
    格式: US.AAPL240119C175000 或 AAPL240119C175000
    
    Returns:
        {
            'underlying': 'AAPL',
            'expiry_date': datetime,
            'option_type': 'C' or 'P',
            'strike': 175.0
        }
    """
    # 移除US.前缀
    clean_symbol = symbol.replace('US.', '')
    
    # 正则匹配: TICKER + YYMMDD + C/P + STRIKE*1000
    pattern = r'^([A-Z]+)(\d{6})([CP])(\d+)$'
    match = re.match(pattern, clean_symbol)
    
    if not match:
        return None
    
    ticker, date_str, opt_type, strike_str = match.groups()
    
    try:
        # 解析日期 YYMMDD
        expiry_date = datetime.strptime(date_str, '%y%m%d')
        # 解析行权价 (除以1000)
        strike = float(strike_str) / 1000.0
        
        return {
            'underlying': ticker,
            'expiry_date': expiry_date,
            'option_type': opt_type,
            'strike': strike
        }
    except Exception as e:
        logger.warning(f"解析期权代码失败 {symbol}: {e}")
        return None


def query_symbol_history(symbol: str, store: TradingStore, lookback_days: int = 90) -> pd.DataFrame:
    """
    查询标的的历史盈亏记录
    
    Args:
        symbol: 期权代码
        store: TradingStore实例
        lookback_days: 回看天数
    
    Returns:
        DataFrame with columns: [created_at, pnl]
    """
    try:
        # 查询该标的的历史订单
        cutoff_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
        
        query = """
        SELECT 
            created_at,
            side,
            filled_quantity,
            average_fill_price
        FROM order_intents
        WHERE symbol = ?
          AND status = 'FILLED'
          AND strategy_id = 'volatility_long_gamma'
          AND DATE(created_at) >= ?
        ORDER BY created_at ASC
        """
        
        with store.connection() as connection:
            rows = connection.execute(query, (symbol, cutoff_date)).fetchall()
            rows = [dict(row) for row in rows]
        
        if not rows:
            return pd.DataFrame(columns=['created_at', 'pnl'])
        
        # 计算每笔交易的盈亏
        df = pd.DataFrame(rows, columns=['created_at', 'side', 'filled_quantity', 'average_fill_price'])
        df['created_at'] = pd.to_datetime(df['created_at'])
        
        # 按日期分组计算每日净盈亏
        daily_pnl = []
        for date, group in df.groupby(df['created_at'].dt.date):
            buy_amount = group[group['side'] == 'BUY']['filled_quantity'] * \
                        group[group['side'] == 'BUY']['average_fill_price']
            sell_amount = group[group['side'] == 'SELL']['filled_quantity'] * \
                         group[group['side'] == 'SELL']['average_fill_price']
            
            net_pnl = sell_amount.sum() - buy_amount.sum()
            daily_pnl.append({
                'created_at': pd.Timestamp(date),
                'pnl': net_pnl
            })
        
        return pd.DataFrame(daily_pnl)
        
    except Exception as e:
        logger.warning(f"查询 {symbol} 历史失败: {e}")
        return pd.DataFrame(columns=['created_at', 'pnl'])


def prepare_symbol_features(
    symbol: str, 
    trade_time: datetime,
    store: TradingStore,
    broker = None  # 可选的broker实例，用于获取市场数据
) -> dict:
    """
    准备ML特征
    
    Args:
        symbol: 期权代码
        trade_time: 交易时间
        store: TradingStore实例
        broker: 可选的broker实例，用于获取VIX/SPY数据
    
    Returns:
        特征字典
    """
    features = {}
    
    # 1. 期权特征
    opt_info = parse_option_symbol(symbol)
    if opt_info:
        # 移除时区信息进行日期计算
        trade_dt_naive = trade_time.replace(tzinfo=None) if trade_time.tzinfo else trade_time
        expiry_dt = opt_info['expiry_date']
        
        features['days_to_expiry'] = (expiry_dt - trade_dt_naive).days
        features['is_call'] = 1 if opt_info['option_type'] == 'C' else 0
        features['strike_price'] = opt_info['strike']
        
        # 到期分桶
        if features['days_to_expiry'] <= 7:
            features['expiry_bucket'] = 1
        elif features['days_to_expiry'] <= 14:
            features['expiry_bucket'] = 2
        elif features['days_to_expiry'] <= 30:
            features['expiry_bucket'] = 3
        else:
            features['expiry_bucket'] = 4
    else:
        # 默认值
        features['days_to_expiry'] = 30
        features['is_call'] = 1
        features['strike_price'] = 0
        features['expiry_bucket'] = 3
    
    # 2. 历史统计（从数据库查询）
    history = query_symbol_history(symbol, store)
    
    if len(history) >= 3:
        hist_pnl = history['pnl']
        features['symbol_avg_pnl'] = float(hist_pnl.mean())
        features['symbol_std_pnl'] = float(hist_pnl.std())
        features['symbol_max_pnl'] = float(hist_pnl.max())
        features['symbol_min_pnl'] = float(hist_pnl.min())
        features['symbol_win_rate'] = float((hist_pnl > 0).mean())
        
        # 最近3天和7天
        recent_3d = hist_pnl.tail(3)
        features['symbol_3d_avg_pnl'] = float(recent_3d.mean()) if len(recent_3d) > 0 else 0
        
        recent_7d = hist_pnl.tail(7)
        features['symbol_7d_avg_pnl'] = float(recent_7d.mean()) if len(recent_7d) > 0 else 0
        
        features['symbol_trade_count'] = len(history)
        
        # 趋势
        if len(history) >= 6:
            recent = hist_pnl.tail(3).mean()
            earlier = hist_pnl.iloc[:-3].mean()
            features['symbol_pnl_trend'] = float(recent - earlier)
        else:
            features['symbol_pnl_trend'] = 0
    else:
        # 默认值
        features['symbol_avg_pnl'] = 0
        features['symbol_std_pnl'] = 0
        features['symbol_max_pnl'] = 0
        features['symbol_min_pnl'] = 0
        features['symbol_win_rate'] = 0.5
        features['symbol_3d_avg_pnl'] = 0
        features['symbol_7d_avg_pnl'] = 0
        features['symbol_trade_count'] = 0
        features['symbol_pnl_trend'] = 0
    
    # 3. 时间特征
    features['day_of_week'] = trade_time.weekday()
    features['hour_of_day'] = trade_time.hour
    features['is_market_close'] = 1 if trade_time.hour >= 15 else 0
    
    # 4. 市场环境特征（如果提供了broker）
    if broker is not None:
        try:
            from .market_env import get_market_environment
            market_data = get_market_environment(broker)
            features['vix_level'] = market_data['vix_level']
            features['spy_return_1d'] = market_data['spy_return_1d']
            features['spy_return_5d'] = market_data['spy_return_5d']
            features['market_regime'] = market_data['market_regime']
        except Exception as e:
            logger.warning(f"获取市场环境数据失败: {e}，使用默认值")
            features['vix_level'] = 20.0
            features['spy_return_1d'] = 0.0
            features['spy_return_5d'] = 0.0
            features['market_regime'] = 1
    else:
        # 占位符
        features['vix_level'] = 20.0
        features['spy_return_1d'] = 0.0
        features['spy_return_5d'] = 0.0
        features['market_regime'] = 1
    
    return features


def predict_symbol_score(
    symbol: str, 
    model, 
    feature_cols: list[str],
    store: TradingStore,
    trade_time: datetime | None = None,
    broker = None  # 新增broker参数
) -> float:
    """
    预测标的评分
    
    Args:
        symbol: 期权代码
        model: 训练好的模型
        feature_cols: 特征列表
        store: TradingStore实例
        trade_time: 交易时间，默认当前时间
        broker: 可选的broker实例，用于获取市场数据
    
    Returns:
        0-1之间的概率，越高越可能盈利
    """
    if trade_time is None:
        trade_time = datetime.now()
    
    try:
        # 准备特征（传入broker）
        features = prepare_symbol_features(symbol, trade_time, store, broker)
        
        # 转为DataFrame
        feature_df = pd.DataFrame([features])[feature_cols]
        
        # 预测
        proba = model.predict_proba(feature_df)[0, 1]
        
        return float(proba)
        
    except Exception as e:
        logger.error(f"预测失败 {symbol}: {e}", exc_info=True)
        return 0.0  # 失败时返回最低分
        trade_time = datetime.now()
    
    try:
        # 准备特征
        features = prepare_symbol_features(symbol, trade_time, store)
        
        # 转为DataFrame
        feature_df = pd.DataFrame([features])[feature_cols]
        
        # 预测
        proba = model.predict_proba(feature_df)[0, 1]
        
        return float(proba)
        
    except Exception as e:
        logger.error(f"预测失败 {symbol}: {e}", exc_info=True)
        return 0.0  # 失败时返回最低分


def batch_predict_scores(
    symbols: list[str],
    model,
    feature_cols: list[str],
    store: TradingStore,
    trade_time: datetime | None = None,
    broker = None  # 新增broker参数
) -> dict[str, float]:
    """
    批量预测标的评分
    
    Returns:
        {symbol: score} 字典
    """
    if trade_time is None:
        trade_time = datetime.now()
    
    scores = {}
    for symbol in symbols:
        try:
            score = predict_symbol_score(symbol, model, feature_cols, store, trade_time, broker)
            scores[symbol] = score
        except Exception as e:
            logger.warning(f"批量预测 {symbol} 失败: {e}")
            scores[symbol] = 0.0
    
    return scores
