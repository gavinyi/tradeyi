#!/usr/bin/env python3
"""
导出波动率策略的历史交易数据，用于机器学习训练

用法:
    uv run python scripts/ml_export_training_data.py \
        --output data/ml_training/volatility_trades.csv \
        --start-date 2026-06-01
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


def _print_safe(*args, **kwargs) -> None:
    """打印时容忍非 UTF-8 控制台（Windows GBK 双击/命令行会踩）。

    emoji 在 GBK 流上抛 `UnicodeEncodeError`：手工跑脚本会直接崩，`pytest -s` 也会假红。
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        fallback = " ".join(str(arg) for arg in args)
        print(fallback.encode("ascii", "replace").decode("ascii"), **kwargs)


def export_trades(db_path: str, start_date: str | None = None) -> pd.DataFrame:
    """
    从trading.db导出历史交易数据
    
    Args:
        db_path: 数据库路径
        start_date: 起始日期 (YYYY-MM-DD)
    
    Returns:
        交易数据DataFrame
    """
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
    
    # 注意：我们会在后续处理中移除 average_fill_price 和 filled_quantity
    # 因为这些是订单执行后的结果特征，不应用于训练
    
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    # 解析时间
    df['created_at'] = pd.to_datetime(df['created_at'])
    df['trade_date'] = df['created_at'].dt.date
    
    _print_safe(f"✅ 导出了 {len(df)} 条交易记录")
    _print_safe(f"📅 时间范围: {df['trade_date'].min()} 至 {df['trade_date'].max()}")
    _print_safe(f"📊 标的数量: {df['symbol'].nunique()}")
    
    return df


def calculate_pnl_labels(df: pd.DataFrame, forward_days: int = 7) -> pd.DataFrame:
    """
    为每个标的计算未来N天的盈亏标签
    
    Args:
        df: 交易数据
        forward_days: 向前看多少天
    
    Returns:
        添加了标签的DataFrame
    """
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
    
    _print_safe(f"\n📈 标签统计 (未来{forward_days}天):")
    print(f"  盈利样本: {pos_count} ({pos_count/len(result_df)*100:.1f}%)")
    print(f"  亏损样本: {neg_count} ({neg_count/len(result_df)*100:.1f}%)")
    
    return result_df


def calculate_historical_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算每条记录的历史统计特征
    
    Args:
        df: 标注好的交易数据
    
    Returns:
        添加了特征的DataFrame
    """
    _print_safe("\n🔧 计算历史特征...")
    
    # 按标的和时间排序
    df = df.sort_values(['symbol', 'created_at']).reset_index(drop=True)
    
    features = []
    
    for symbol, group in df.groupby('symbol'):
        group = group.reset_index(drop=True)
        
        for idx in range(len(group)):
            record = group.loc[idx].to_dict()
            
            # 历史特征：只使用当前记录之前的数据
            history = group.iloc[:idx]
            
            if len(history) >= 5:
                # 历史收益特征
                hist_pnl = history['window_net_pnl']
                record['symbol_avg_pnl'] = hist_pnl.mean()
                record['symbol_std_pnl'] = hist_pnl.std()
                record['symbol_max_pnl'] = hist_pnl.max()
                record['symbol_min_pnl'] = hist_pnl.min()
                record['symbol_win_rate'] = (hist_pnl > 0).mean()
                
                # 最近7天收益
                recent_7d = history.tail(7)['window_net_pnl']
                record['symbol_7d_avg_pnl'] = recent_7d.mean() if len(recent_7d) > 0 else 0
                
                # 交易频率
                record['symbol_trade_count'] = len(history)
            else:
                # 样本不足，填充默认值
                record['symbol_avg_pnl'] = 0
                record['symbol_std_pnl'] = 0
                record['symbol_max_pnl'] = 0
                record['symbol_min_pnl'] = 0
                record['symbol_win_rate'] = 0.5
                record['symbol_7d_avg_pnl'] = 0
                record['symbol_trade_count'] = len(history)
            
            # 时间特征
            dt = pd.to_datetime(record['created_at'])
            record['day_of_week'] = dt.dayofweek  # 0=Monday, 4=Friday
            record['hour_of_day'] = dt.hour
            
            features.append(record)
    
    result_df = pd.DataFrame(features)
    
    _print_safe(f"✅ 计算完成，共 {len(result_df)} 条特征记录")
    
    return result_df


def main():
    parser = argparse.ArgumentParser(description='导出波动率策略训练数据')
    parser.add_argument('--output', required=True, help='输出CSV文件路径')
    parser.add_argument('--db', default='data/trading.db', help='数据库路径')
    parser.add_argument('--start-date', help='起始日期 (YYYY-MM-DD)')
    parser.add_argument('--forward-days', type=int, default=7, help='向前预测天数')
    
    args = parser.parse_args()
    
    # 创建输出目录
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    _print_safe("📂 导出波动率策略训练数据")
    print(f"数据库: {args.db}")
    print(f"输出路径: {args.output}")
    print(f"预测窗口: {args.forward_days}天\n")
    
    # 1. 导出原始交易数据
    df = export_trades(args.db, args.start_date)
    
    if df.empty:
        _print_safe("❌ 没有找到交易数据")
        return
    
    # 2. 计算盈亏标签
    labeled_df = calculate_pnl_labels(df, args.forward_days)
    
    # 3. 计算历史特征
    featured_df = calculate_historical_features(labeled_df)
    
    # 4. 保存结果
    featured_df.to_csv(args.output, index=False)
    _print_safe(f"\n✅ 数据已保存到: {args.output}")
    
    # 5. 数据质量报告
    _print_safe("\n📊 数据质量报告:")
    print(f"  总样本数: {len(featured_df)}")
    print(f"  特征列数: {len(featured_df.columns)}")
    print(f"  缺失值比例: {featured_df.isnull().sum().sum() / featured_df.size * 100:.2f}%")
    print(f"  正样本比例: {(featured_df['is_profitable'] == 1).mean() * 100:.1f}%")
    
    # 按标的统计
    symbol_stats = featured_df.groupby('symbol').agg({
        'is_profitable': ['count', 'mean'],
        'window_net_pnl': 'mean'
    }).round(3)
    symbol_stats.columns = ['样本数', '胜率', '平均盈亏']
    
    _print_safe("\n📈 TOP 10 最佳标的:")
    print(symbol_stats.sort_values('平均盈亏', ascending=False).head(10))
    
    _print_safe("\n📉 TOP 10 最差标的:")
    print(symbol_stats.sort_values('平均盈亏', ascending=True).head(10))


if __name__ == "__main__":
    main()
