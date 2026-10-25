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


class MissingFeatureError(RuntimeError):
    """推理侧拿不到模型所需特征（例如缺行情上下文）。

    必须显式失败：`predict_symbol_score` 以前用 `except Exception: return 0.0`
    把它吞成「最低分」，结果是模型静默失效、所有标的同时被压到 0.0，
    日志里只剩一行 error —— 这正是要消灭的静默降级形态。
    """


def moneyness_ratio(spot: float | None, strike: float | None) -> float | None:
    """虚实值偏离度 moneyness = S/K - 1（v3.2 定义）。

    训练侧与推理侧**必须**引用这同一个函数：以前训练用 `strike/median(strike)-1`
    顶替，是随当天候选池漂移、换一天值就变的伪特征，推理侧根本无法复现。
    """
    try:
        spot_value = float(spot or 0)
        strike_value = float(strike or 0)
    except (TypeError, ValueError):
        return None
    if spot_value <= 0 or strike_value <= 0:
        return None
    return spot_value / strike_value - 1.0


def gamma_to_theta_ratio(gamma: float | None, theta: float | None) -> float | None:
    """做多波动率凸性性价比 gamma_to_theta = |gamma| / (|theta| + 1e-6)（v3.2 定义）。"""
    try:
        gamma_value = abs(float(gamma))
        theta_value = abs(float(theta))
    except (TypeError, ValueError):
        return None
    if gamma_value <= 0 or theta_value <= 0:
        return None
    return gamma_value / (theta_value + 1e-6)


# 推理侧**真实可产**的特征全集；模型 `feature_cols` 必须是它的子集。
# 单列成常量是刻意的：训练侧 / `load_ml_model` 签名校验 / 守卫测试都引用同一份定义，
# 防止「训练加了新特征、推理没跟上 → 预测 KeyError → 静默 0.0」再次发生。
INFERENCE_FEATURE_KEYS = frozenset({
    # 期权静态特征
    'days_to_expiry', 'is_call', 'strike_price', 'expiry_bucket',
    # 历史统计
    'symbol_avg_pnl', 'symbol_std_pnl', 'symbol_max_pnl', 'symbol_min_pnl',
    'symbol_win_rate', 'symbol_3d_avg_pnl', 'symbol_7d_avg_pnl',
    'symbol_trade_count', 'symbol_pnl_trend',
    # 时间
    'day_of_week', 'hour_of_day', 'is_market_close', 'month', 'quarter',
    # 市场环境（无 broker 时为确定性占位值）
    'vix_level', 'spy_return_1d', 'spy_return_5d', 'market_regime',
    # v3.2 凸性/无量纲（需要 market_context 里的真实行情）
    'moneyness', 'gamma_to_theta',
    # 标的级新信息（零采集：行情仓库/实时 K 线派生；见 data/qa/symbol_level_feature_availability.md）
    'symbol_momentum_20d', 'regime_label',
})


def momentum_20d_from_bars(bars, *, include_last: bool = False) -> float | None:
    """标的 20 日动量（严格用**已收**K 线，防 look-ahead）。

    - ``include_last=False``（实时推理）：最后一根可能是未收盘的当根，用 ``close[-2]/close[-22]-1``；
    - ``include_last=True``（历史回补，bars 已按成交日截断）：用 ``close[-1]/close[-21]-1``。
    数据不足返回 None，绝不编 0（没数据与"动量恰好为 0"是两件事）。
    """
    need = 21 if include_last else 22
    if bars is None or len(bars) < need:
        return None
    try:
        closes = [float(value) for value in bars["close"].tolist()]
    except (KeyError, TypeError, ValueError):
        return None
    base = closes[-21] if include_last else closes[-22]
    latest = closes[-1] if include_last else closes[-2]
    if base <= 0:
        return None
    return latest / base - 1.0


def regime_label_from_spy(spy_return_5d: float | None, threshold: float = 0.01) -> int | None:
    """大盘趋势标签：+1=TREND_UP / -1=TREND_DOWN / 0=RANGE。

    与既有 ``market_regime``（1/2/3 = 正常/高波动/危机，波动维度）互补：
    这里给的是**方向维度**。零采集，复用 SPY 5 日收益。
    """
    if spy_return_5d is None:
        return None
    try:
        value = float(spy_return_5d)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


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
            model = model_data['model']
            feature_cols = list(model_data.get('feature_cols') or [])
            if not feature_cols:
                raise ValueError("模型文件缺少 feature_cols，无法做特征签名校验")
            unsupported = [col for col in feature_cols if col not in INFERENCE_FEATURE_KEYS]
            if unsupported:
                # 静默降级防线：加载一个推理侧造不全特征的模型，等于让每次预测
                # 都在 `[feature_cols]` 上 KeyError，然后被吞成 0.0（全标的失效）。
                logger.error(
                    "❌ 拒绝加载 ML 模型 %s：特征 %s 无法在推理侧构造；"
                    "继续加载会导致预测 KeyError→0.0（静默降级）。已回退内置规则模型。",
                    model_path, unsupported,
                )
                return BuiltinRuleModel(), default_cols
            if hasattr(model, 'n_jobs'):
                model.n_jobs = 1
            logger.info(f"✅ 加载ML模型: {model_path} (强制 n_jobs=1 单线程安全模式)")
            return model, feature_cols
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
    broker = None,  # 可选的broker实例，用于获取市场数据
    market_context: dict | None = None,
) -> dict:
    """
    准备ML特征
    
    Args:
        symbol: 期权代码
        trade_time: 交易时间
        store: TradingStore实例
        broker: 可选的broker实例，用于获取VIX/SPY数据
        market_context: v3.2 凸性特征所需的真实行情，键为
            ``spot`` / ``option_gamma`` / ``option_theta``。
            缺哪项就不产哪个键（绝不编造 0），由 predict_symbol_score 显式报错。
    
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
    # v3.2 candidate 模型（18 特征）含 month，推理侧此前没有 → KeyError→0.0
    features['month'] = trade_time.month
    features['quarter'] = (trade_time.month - 1) // 3 + 1
    
    # 4. 市场环境特征（如果提供了broker）
    if broker is not None:
        try:
            from .market_env import get_market_environment
            market_data = get_market_environment(broker)
            features['vix_level'] = market_data['vix_level']
            features['spy_return_1d'] = market_data['spy_return_1d']
            features['spy_return_5d'] = market_data['spy_return_5d']
            features['market_regime'] = market_data['market_regime']
            trend_label = regime_label_from_spy(market_data['spy_return_5d'])
            if trend_label is not None:
                features['regime_label'] = trend_label
        except Exception as e:
            logger.warning(f"获取市场环境数据失败: {e}，使用默认值")
            features['vix_level'] = 20.0
            features['spy_return_1d'] = 0.0
            features['spy_return_5d'] = 0.0
            features['market_regime'] = 1
    else:
        # 占位符（仅当调用方拿不到 broker 时；这也是要修「推理链不传 broker」的原因）
        features['vix_level'] = 20.0
        features['spy_return_1d'] = 0.0
        features['spy_return_5d'] = 0.0
        features['market_regime'] = 1

    # 5. v3.2 凸性/无量纲特征：只认真实行情。
    # 缺 spot / 希腊字母时**不写这两个键**，由 predict_symbol_score 报 MissingFeatureError；
    # 编一个 0 出去比报错危险得多（模型会把「没数据」学成「低赔率」）。
    context = market_context or {}
    moneyness = moneyness_ratio(context.get('spot'), features.get('strike_price'))
    if moneyness is not None:
        features['moneyness'] = moneyness
    gamma_to_theta = gamma_to_theta_ratio(
        context.get('option_gamma'), context.get('option_theta')
    )
    if gamma_to_theta is not None:
        features['gamma_to_theta'] = gamma_to_theta

    # 6. 标的级新信息（零采集）：优先用调用方已取到的行情（避免逐标的重复拉 K 线）
    momentum = context.get('symbol_momentum_20d')
    if momentum is None and broker is not None and opt_info is not None:
        try:
            momentum = momentum_20d_from_bars(
                broker.candles(f"US.{opt_info['underlying']}", count=120)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"获取 {symbol} 标的动量失败: {exc}")
            momentum = None
    if momentum is not None:
        features['symbol_momentum_20d'] = float(momentum)

    return features


def predict_symbol_score(
    symbol: str,
    model,
    feature_cols: list[str],
    store: TradingStore,
    trade_time: datetime | None = None,
    broker=None,
    market_context: dict | None = None,
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
        market_context: v3.2 凸性特征所需的真实行情（spot / option_gamma / option_theta）

    Returns:
        0-1之间的概率，越高越可能盈利

    Raises:
        MissingFeatureError: 模型所需特征在推理侧拿不到。以前这里
            ``except Exception: return 0.0`` 把「没数据」吞成「全标的 0.0」，
            那正是静默降级；现在必须显式失败，由调用方回退到规则排序。
    """
    if trade_time is None:
        trade_time = datetime.now()

    features = prepare_symbol_features(
        symbol, trade_time, store, broker, market_context
    )
    missing = [col for col in feature_cols if col not in features]
    if missing:
        raise MissingFeatureError(
            f"{symbol} 缺少模型所需特征 {missing}；推理侧无法构造，拒绝用 0.0 兜底"
        )

    feature_df = pd.DataFrame([features])[feature_cols]
    proba = model.predict_proba(feature_df)[0, 1]
    return float(proba)


def batch_predict_scores(
    symbols: list[str],
    model,
    feature_cols: list[str],
    store: TradingStore,
    trade_time: datetime | None = None,
    broker=None,
    market_context: dict | None = None,
) -> dict[str, float | None]:
    """
    批量预测标的评分

    ``market_context`` 是**共用**上下文（如当日 spot）；逐合约的希腊字母需要逐个
    调用 :func:`predict_symbol_score` / ``MLSymbolScorer.score_symbol``。

    Returns:
        {symbol: score} 字典；拿不到必需特征的标的给 ``None``（NA），
        不再用 0.0 冒充「最低分」—— 0.0 会被下游当成真实评估结果。
    """
    if trade_time is None:
        trade_time = datetime.now()

    scores: dict[str, float | None] = {}
    for symbol in symbols:
        try:
            scores[symbol] = predict_symbol_score(
                symbol, model, feature_cols, store, trade_time, broker, market_context
            )
        except MissingFeatureError as exc:
            logger.error(f"批量预测 {symbol} 缺必需特征，评分置 NA（不再静默 0.0）：{exc}")
            scores[symbol] = None
        except Exception as exc:  # noqa: BLE001
            logger.error(f"批量预测 {symbol} 失败，评分置 NA：{exc}")
            scores[symbol] = None

    return scores
