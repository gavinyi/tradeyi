"""
实时市场环境数据获取
用于ML特征：VIX、SPY涨跌幅、市场状态
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .broker import FutuPaperBroker

logger = logging.getLogger(__name__)


def get_vix_level(broker: FutuPaperBroker) -> float:
    """
    获取VIX当前水平
    
    Args:
        broker: Futu broker实例
    
    Returns:
        VIX水平，获取失败返回20.0（默认值）
    """
    try:
        # 尝试从broker获取VIX报价
        snapshot = broker.quote_snapshot(['US.VIX'])
        
        if snapshot and len(snapshot) > 0:
            vix_data = snapshot[0]
            last_price = vix_data.get('last_price', 20.0)
            logger.info(f"✅ VIX当前水平: {last_price:.2f}")
            return float(last_price)
        else:
            logger.warning("⚠️  VIX数据为空，使用默认值20.0")
            return 20.0
            
    except Exception as e:
        logger.warning(f"⚠️  获取VIX失败: {e}，使用默认值20.0")
        return 20.0


def get_spy_returns(broker: FutuPaperBroker, days: int = 5) -> dict[str, float]:
    """
    获取SPY的近期涨跌幅
    
    Args:
        broker: Futu broker实例
        days: 回看天数
    
    Returns:
        {'1d': 0.01, '5d': 0.03} 涨跌幅字典
    """
    try:
        # 获取SPY历史K线
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days+5)  # 多取几天以防假期
        
        klines = broker.get_history_kline(
            'US.SPY',
            start_date.strftime('%Y-%m-%d'),
            end_date.strftime('%Y-%m-%d'),
            ktype='K_DAY'
        )
        
        if not klines or len(klines) < 2:
            logger.warning("⚠️  SPY历史数据不足，使用默认值")
            return {'1d': 0.0, '5d': 0.0}
        
        # 计算涨跌幅
        closes = [k['close'] for k in klines]
        
        return_1d = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 else 0.0
        return_5d = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0.0
        
        logger.info(f"✅ SPY涨跌: 1日={return_1d:.2%}, 5日={return_5d:.2%}")
        
        return {
            '1d': float(return_1d),
            '5d': float(return_5d)
        }
        
    except Exception as e:
        logger.warning(f"⚠️  获取SPY涨跌失败: {e}，使用默认值")
        return {'1d': 0.0, '5d': 0.0}


def get_market_regime(vix_level: float, spy_return_5d: float) -> int:
    """
    判断市场状态
    
    Args:
        vix_level: VIX水平
        spy_return_5d: SPY 5日涨跌幅
    
    Returns:
        1=正常, 2=高波动, 3=危机
    """
    if vix_level > 30 or abs(spy_return_5d) > 0.05:
        return 3  # 危机
    elif vix_level > 20 or abs(spy_return_5d) > 0.03:
        return 2  # 高波动
    else:
        return 1  # 正常


def get_market_environment(broker: FutuPaperBroker) -> dict[str, float]:
    """
    获取完整的市场环境数据
    
    Returns:
        {
            'vix_level': 20.0,
            'spy_return_1d': 0.01,
            'spy_return_5d': 0.03,
            'market_regime': 1
        }
    """
    try:
        vix = get_vix_level(broker)
        spy_returns = get_spy_returns(broker, days=5)
        regime = get_market_regime(vix, spy_returns['5d'])
        
        return {
            'vix_level': vix,
            'spy_return_1d': spy_returns['1d'],
            'spy_return_5d': spy_returns['5d'],
            'market_regime': regime
        }
        
    except Exception as e:
        logger.error(f"❌ 获取市场环境失败: {e}")
        return {
            'vix_level': 20.0,
            'spy_return_1d': 0.0,
            'spy_return_5d': 0.0,
            'market_regime': 1
        }
