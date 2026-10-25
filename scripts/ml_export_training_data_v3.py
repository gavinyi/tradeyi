#!/usr/bin/env python3
"""
导出波动率策略的历史交易数据 - V3增强版
新增功能:
1. 市场数据特征 (正股价格、波动率、成交量)
2. 期权特征 (到期天数、实值/虚值程度)
3. 市场环境特征 (VIX、大盘走势)

用法:
    uv run python scripts/ml_export_training_data_v3.py \
        --output data/ml_training/volatility_trades_v3.csv \
        --start-date 2026-07-01
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np


def _force_utf8_stdio() -> None:
    """把 stdout/stderr 换成 UTF-8 包装（Windows 控制台默认 GBK，emoji 会直接崩）。

    **只能在 `__main__` 里调用**，绝不能在 import 期执行：import 期替换
    `sys.stdout`/`sys.stderr` 会把 pytest 的 capture 对象顶掉，teardown 去读已关闭
    的文件 → `ValueError: I/O operation on closed file`（`ml_retrain_monthly.py`
    2026-10-25 已踩过同一个坑）。
    """
    if sys.platform != "win32":
        return
    import io

    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


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
    except:
        return None


def export_trades(db_path: str, start_date: str | None = None) -> pd.DataFrame:
    """从trading.db导出历史交易数据"""
    conn = sqlite3.connect(db_path)
    
    # 构建查询条件
    where_clause = "WHERE strategy_id = 'volatility_long_gamma' AND status = 'FILLED'"
    if start_date:
        where_clause += f" AND DATE(created_at) >= '{start_date}'"
    
    # 导出订单数据
    query = f"""
    SELECT 
        created_at,
        strategy_id,
        symbol,
        side,
        quantity,
        filled_quantity,
        average_fill_price,
        status,
        metadata_json
    FROM order_intents
    {where_clause}
    ORDER BY created_at ASC
    """
    
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    # 解析时间
    df['created_at'] = pd.to_datetime(df['created_at'])
    df['trade_date'] = df['created_at'].dt.date
    
    print(f"✅ 导出了 {len(df)} 条交易记录")
    print(f"📅 时间范围: {df['trade_date'].min()} 至 {df['trade_date'].max()}")
    print(f"📊 标的数量: {df['symbol'].nunique()}")
    
    return df


def add_option_features(df: pd.DataFrame) -> pd.DataFrame:
    """添加期权特征"""
    print("\n🔧 解析期权特征...")
    
    option_features = []
    
    for idx, row in df.iterrows():
        features = {}
        
        # 解析期权代码
        opt_info = parse_option_symbol(row['symbol'])
        
        if opt_info:
            trade_dt = pd.to_datetime(row['created_at'])
            # 移除时区信息进行日期计算
            trade_dt_naive = trade_dt.replace(tzinfo=None) if trade_dt.tzinfo else trade_dt
            expiry_dt = opt_info['expiry_date']
            
            # 到期天数
            features['days_to_expiry'] = (expiry_dt - trade_dt_naive).days
            
            # 期权类型 (C=1, P=0)
            features['is_call'] = 1 if opt_info['option_type'] == 'C' else 0
            
            # 行权价
            features['strike_price'] = opt_info['strike']
            
            # 标的代码
            features['underlying_symbol'] = opt_info['underlying']
            
            # 到期周 (1-4周分类)
            if features['days_to_expiry'] <= 7:
                features['expiry_bucket'] = 1
            elif features['days_to_expiry'] <= 14:
                features['expiry_bucket'] = 2
            elif features['days_to_expiry'] <= 30:
                features['expiry_bucket'] = 3
            else:
                features['expiry_bucket'] = 4
        else:
            # 无法解析，填充默认值
            features['days_to_expiry'] = 30
            features['is_call'] = 1
            features['strike_price'] = 0
            features['underlying_symbol'] = 'UNKNOWN'
            features['expiry_bucket'] = 3
        
        option_features.append(features)
    
    # 合并到原数据
    option_df = pd.DataFrame(option_features)
    result = pd.concat([df.reset_index(drop=True), option_df], axis=1)
    
    print(f"✅ 解析了 {len(result)} 个期权特征")
    print(f"   平均到期天数: {result['days_to_expiry'].mean():.1f}")
    print(f"   Call比例: {result['is_call'].mean()*100:.1f}%")
    
    return result


MARKET_TIMEZONE = "America/New_York"
# 行情快照与成交时刻的最大允许时差。原为 1 天是错的：跨夜匹配会把 24 小时前的
# gamma/theta 当成成交时刻行情（Reviewer/Ops 2026-10-25），3a 三组对比就全建在
# 假对齐上。收窄到 45 分钟（覆盖扫描→下单的正常延迟），超容差样本直接丢弃并计数：
# 丢掉的样本本来就无法代表入场时点，宁缺毋滥。
MARKET_TOLERANCE_MINUTES = 45


def _market_stamp_to_utc(raw) -> pd.Timestamp:
    """把一条行情时间解释成 UTC-aware。

    `option_history.time_key` 是 **naive 的交易所本地时间（ET）**（与
    `futu_quant.data_quality.quote_freshness` 同一口径：naive → localize(NY)）；
    `order_intents.created_at` 是 UTC-aware。两者必须先归一到同一时区形态：
    - 直接混算会抛 `MergeError: incompatible merge keys`；
    - 更危险的是"能算但不报错"的情形：naive 当 UTC 会错位 4~5 小时，把**成交之后**
      的快照当成入场时点 —— 典型 look-ahead 污染。
    """
    stamp = pd.to_datetime(raw, errors="coerce")
    if pd.isna(stamp):
        return pd.NaT
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(MARKET_TIMEZONE)
    return stamp.tz_convert("UTC")


def _market_times_to_utc(values: pd.Series) -> pd.Series:
    return pd.Series(
        [_market_stamp_to_utc(value) for value in values],
        index=values.index,
        dtype="datetime64[ns, UTC]",
    )


def _print_safe(message: str) -> None:
    """打印时容忍非 UTF-8 控制台（Windows GBK、`pytest -s` 都会踩）。

    emoji 在 GBK 流上会抛 `UnicodeEncodeError`：手工跑导出会崩，`-s` 跑守卫会
    变成 6 条假红。入口已经接管 stdio，但**被测试直接调用的函数**仍需自带兜底。
    """
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"))


def add_option_market_features(df: pd.DataFrame, db_path: str) -> pd.DataFrame:
    """合并``option_history`` 里**成交时刻的真实行情**：正股价 + 期权希腊字母。

    为什么必须这么做（v3.2 特征定义之争）：moneyness = S/K - 1 与
    gamma_to_theta = |gamma|/(|theta|+1e-6) 只能来自真行情。以前训练侧用
    ``strike / median(同标的 strike) - 1``（随当天候选池漂移、同一合约换一天就变）
    和 ``1/(dte·vix)``（与 gamma/theta 毫无关系）顶替，推理侧永远复现不出来，
    训练出的重要性也是伪特征的成绩。缺行情就**丢样本并告警**，绝不补 0 或补伪值。

    时差上限见 :data:`MARKET_TOLERANCE_MINUTES`：只用成交时刻**之前**、且相距不超过
    45 分钟的快照；跨夜 24 小时前的快照会被丢弃（丢多少会打印出来）。
    """
    _print_safe("\n🔧 合并成交时刻真实行情（option_history）...")
    market_cols = ["underlying_price", "option_gamma", "option_theta"]
    conn = sqlite3.connect(db_path)
    try:
        history = pd.read_sql_query(
            "SELECT code, time_key, underlying_price, gamma, theta FROM option_history",
            conn,
        )
    finally:
        conn.close()

    if history.empty:
        raise RuntimeError(
            "option_history 无任何行情记录：无法构造真实 moneyness/gamma_to_theta，"
            "拒绝导出会用伪特征占位的训练集"
        )

    history = history.rename(columns={"gamma": "option_gamma", "theta": "option_theta"})
    history["time_key"] = _market_times_to_utc(history["time_key"])
    history = history.dropna(subset=["time_key"]).sort_values("time_key")
    history = history.dropna(subset=["underlying_price"])

    frame = df.copy()
    frame["code"] = frame["symbol"]
    frame["created_at"] = _market_times_to_utc(frame["created_at"])
    frame = frame.dropna(subset=["created_at"]).sort_values("created_at")
    merged = pd.merge_asof(
        frame,
        history[["code", "time_key", *market_cols]],
        left_on="created_at", right_on="time_key", by="code",
        direction="backward",
        tolerance=pd.Timedelta(minutes=MARKET_TOLERANCE_MINUTES),
    )
    merged = merged.drop(columns=["time_key", "code"])

    covered = merged.dropna(subset=market_cols)
    dropped = len(merged) - len(covered)
    if dropped:
        _print_safe(
            f"   ⚠️  {dropped}/{len(merged)} 条样本没有成交时刻的真实行情 → 丢弃（不补伪值）"
        )
    if covered.empty:
        raise RuntimeError(
            "没有任何样本能匹配到 option_history 的真实行情：v3.2 特征无法构造，拒绝训练"
        )
    _print_safe(f"   ✅ 真实行情样本: {len(covered)}/{len(merged)}")
    return covered.reset_index(drop=True)


def calculate_pnl_labels(df: pd.DataFrame, forward_days: int = 7) -> pd.DataFrame:
    """为每个标的计算未来N天的盈亏标签"""
    labeled_data = []
    
    # 按标的分组
    for symbol, group in df.groupby('symbol'):
        group = group.sort_values('created_at').reset_index(drop=True)
        
        for idx in range(len(group)):
            trade_date = group.loc[idx, 'trade_date']
            end_date = trade_date + timedelta(days=forward_days)
            
            # 计算该时间窗口内的盈亏
            window = group[
                (group['trade_date'] >= trade_date) & 
                (group['trade_date'] <= end_date)
            ]
            
            buy_amount = window[window['side'] == 'BUY']['filled_quantity'] * \
                         window[window['side'] == 'BUY']['average_fill_price']
            sell_amount = window[window['side'] == 'SELL']['filled_quantity'] * \
                          window[window['side'] == 'SELL']['average_fill_price']
            
            net_pnl = sell_amount.sum() - buy_amount.sum()
            
            # 添加标签
            record = group.loc[idx].to_dict()
            record['forward_days'] = forward_days
            record['window_trades'] = len(window)
            record['window_buy_amount'] = buy_amount.sum()
            record['window_sell_amount'] = sell_amount.sum()
            record['window_net_pnl'] = net_pnl
            record['is_profitable'] = 1 if net_pnl > 0 else 0
            
            labeled_data.append(record)
    
    result_df = pd.DataFrame(labeled_data)
    
    # 统计标签分布
    pos_count = (result_df['is_profitable'] == 1).sum()
    neg_count = (result_df['is_profitable'] == 0).sum()
    
    print(f"\n📈 标签统计 (未来{forward_days}天):")
    print(f"  盈利样本: {pos_count} ({pos_count/len(result_df)*100:.1f}%)")
    print(f"  亏损样本: {neg_count} ({neg_count/len(result_df)*100:.1f}%)")
    
    return result_df


def calculate_historical_features(df: pd.DataFrame) -> pd.DataFrame:
    """计算每条记录的历史统计特征"""
    print("\n🔧 计算历史特征...")
    
    # 按标的和时间排序
    df = df.sort_values(['symbol', 'created_at']).reset_index(drop=True)
    
    features = []
    
    for symbol, group in df.groupby('symbol'):
        group = group.reset_index(drop=True)
        
        for idx in range(len(group)):
            record = group.loc[idx].to_dict()
            
            # 历史特征：只使用当前记录之前的数据
            history = group.iloc[:idx]
            
            if len(history) >= 3:  # 降低门槛从5到3
                # 历史收益特征
                hist_pnl = history['window_net_pnl']
                record['symbol_avg_pnl'] = hist_pnl.mean()
                record['symbol_std_pnl'] = hist_pnl.std()
                record['symbol_max_pnl'] = hist_pnl.max()
                record['symbol_min_pnl'] = hist_pnl.min()
                record['symbol_win_rate'] = (hist_pnl > 0).mean()
                
                # 最近3天和7天收益
                recent_3d = history.tail(3)['window_net_pnl']
                record['symbol_3d_avg_pnl'] = recent_3d.mean() if len(recent_3d) > 0 else 0
                
                recent_7d = history.tail(7)['window_net_pnl']
                record['symbol_7d_avg_pnl'] = recent_7d.mean() if len(recent_7d) > 0 else 0
                
                # 交易频率
                record['symbol_trade_count'] = len(history)
                
                # 最近趋势 (最近3笔 vs 之前所有)
                if len(history) >= 6:
                    recent = history.tail(3)['window_net_pnl'].mean()
                    earlier = history.iloc[:-3]['window_net_pnl'].mean()
                    record['symbol_pnl_trend'] = recent - earlier
                else:
                    record['symbol_pnl_trend'] = 0
                    
            else:
                # 样本不足，填充默认值
                record['symbol_avg_pnl'] = 0
                record['symbol_std_pnl'] = 0
                record['symbol_max_pnl'] = 0
                record['symbol_min_pnl'] = 0
                record['symbol_win_rate'] = 0.5
                record['symbol_3d_avg_pnl'] = 0
                record['symbol_7d_avg_pnl'] = 0
                record['symbol_trade_count'] = len(history)
                record['symbol_pnl_trend'] = 0
            
            # 时间特征
            dt = pd.to_datetime(record['created_at'])
            record['day_of_week'] = dt.dayofweek  # 0=Monday, 4=Friday
            record['hour_of_day'] = dt.hour
            record['is_market_close'] = 1 if dt.hour >= 15 else 0  # 收盘前1小时
            
            # 月份特征 (季度效应)
            record['month'] = dt.month
            record['quarter'] = (dt.month - 1) // 3 + 1
            
            features.append(record)
    
    result_df = pd.DataFrame(features)
    
    print(f"✅ 计算完成，共 {len(result_df)} 条特征记录")
    
    return result_df


def add_market_environment_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    添加市场环境特征
    注意: 这需要外部市场数据，当前版本使用占位符
    """
    print("\n🔧 添加市场环境特征...")
    
    # TODO: 从broker或Yahoo Finance获取真实数据
    # 当前使用占位符特征
    
    df['vix_level'] = 20.0  # VIX水平 (需要实时获取)
    df['spy_return_1d'] = 0.0  # SPY 1日涨跌幅
    df['spy_return_5d'] = 0.0  # SPY 5日涨跌幅
    df['market_regime'] = 1  # 市场状态: 1=正常, 2=高波动, 3=危机
    
    print("⚠️  市场特征当前使用占位符，需要接入真实数据源")
    
    return df


def main():
    parser = argparse.ArgumentParser(description='导出波动率策略训练数据 V3')
    parser.add_argument('--output', required=True, help='输出CSV文件路径')
    parser.add_argument('--db', default='data/trading.db', help='数据库路径')
    parser.add_argument('--start-date', help='起始日期 (YYYY-MM-DD)')
    parser.add_argument('--forward-days', type=int, default=7, help='向前预测天数')
    
    args = parser.parse_args()
    
    # 创建输出目录
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    print("📂 导出波动率策略训练数据 V3")
    print(f"数据库: {args.db}")
    print(f"输出路径: {args.output}")
    print(f"预测窗口: {args.forward_days}天\n")
    
    # 1. 导出原始交易数据
    df = export_trades(args.db, args.start_date)
    
    if df.empty:
        print("❌ 没有找到交易数据")
        return
    
    # 2. 添加期权特征
    df = add_option_features(df)
    
    # 3. 计算盈亏标签
    labeled_df = calculate_pnl_labels(df, args.forward_days)
    
    # 4. 计算历史特征
    featured_df = calculate_historical_features(labeled_df)
    
    # 4b. 合并成交时刻真实行情（v3.2 的 moneyness / gamma_to_theta 只认真值）
    featured_df = add_option_market_features(featured_df, args.db)
    
    # 5. 添加市场环境特征
    featured_df = add_market_environment_features(featured_df)
    
    # 6. 保存结果
    featured_df.to_csv(args.output, index=False)
    print(f"\n✅ 数据已保存到: {args.output}")
    
    # 7. 数据质量报告
    print("\n📊 数据质量报告:")
    print(f"  总样本数: {len(featured_df)}")
    print(f"  特征列数: {len(featured_df.columns)}")
    print(f"  缺失值比例: {featured_df.isnull().sum().sum() / featured_df.size * 100:.2f}%")
    print(f"  正样本比例: {(featured_df['is_profitable'] == 1).mean() * 100:.1f}%")
    
    # 新特征统计
    print("\n📈 新特征统计:")
    print(f"  平均到期天数: {featured_df['days_to_expiry'].mean():.1f}")
    print(f"  Call期权占比: {featured_df['is_call'].mean()*100:.1f}%")
    print(f"  收盘前交易占比: {featured_df['is_market_close'].mean()*100:.1f}%")
    
    # 按标的统计
    symbol_stats = featured_df.groupby('underlying_symbol').agg({
        'is_profitable': ['count', 'mean'],
        'window_net_pnl': 'mean'
    }).round(3)
    symbol_stats.columns = ['样本数', '胜率', '平均盈亏']
    
    print("\n📈 TOP 10 最佳标的:")
    print(symbol_stats.sort_values('平均盈亏', ascending=False).head(10))
    
    print("\n📉 TOP 10 最差标的:")
    print(symbol_stats.sort_values('平均盈亏', ascending=True).head(10))


if __name__ == '__main__':
    # 只在作为脚本运行时接管 stdio：Step 2 是手工跑导出，GBK 控制台下 emoji 会
    # 直接 UnicodeEncodeError；import 期做这件事会顶掉 pytest capture（见函数注释）。
    _force_utf8_stdio()
    main()
