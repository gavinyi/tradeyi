from __future__ import annotations

from dataclasses import dataclass
import statistics
import random

import pandas as pd

from .backtest import run_backtest


@dataclass(frozen=True)
class ChronologicalSplit:
    train: slice
    validation: slice
    test: slice


def chronological_splits(
    size: int, train_size: int, validation_size: int, test_size: int, embargo: int = 0,
) -> list[ChronologicalSplit]:
    """Create expanding chronological train/validation/test slices with embargo gaps."""
    values = (size, train_size, validation_size, test_size, embargo)
    if any(not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("窗口长度必须是非负整数")
    if min(train_size, validation_size, test_size) <= 0:
        raise ValueError("训练、验证和测试窗口必须大于 0")
    first_end = train_size + validation_size + test_size + embargo * 2
    if first_end > size:
        raise ValueError(f"历史数据不足：至少需要 {first_end} 行，当前 {size} 行")
    rows = []
    test_stop = first_end
    while test_stop <= size:
        test_start = test_stop - test_size
        validation_stop = test_start - embargo
        validation_start = validation_stop - validation_size
        train_stop = validation_start - embargo
        rows.append(
            ChronologicalSplit(
                train=slice(0, train_stop),
                validation=slice(validation_start, validation_stop),
                test=slice(test_start, test_stop),
            )
        )
        test_stop += test_size
    return rows


def walk_forward_validate(
    bars: pd.DataFrame, strategy: str, initial_cash: float, quantity: int,
    commission: float, slippage_bps: float, windows: int = 4,
) -> dict:
    ordered = bars.sort_values("time_key").reset_index(drop=True)
    minimum = 80
    if len(ordered) < minimum * windows:
        raise ValueError(f"Walk-forward 至少需要 {minimum * windows} 个交易日")
    size = len(ordered) // windows
    rows = []
    for index in range(windows):
        start = index * size
        end = len(ordered) if index == windows - 1 else (index + 1) * size
        sample = ordered.iloc[start:end].copy()
        result = run_backtest(sample, strategy, initial_cash, quantity, commission, slippage_bps)
        rows.append({"窗口": index + 1, "开始": str(sample.iloc[0]["time_key"]),
                     "结束": str(sample.iloc[-1]["time_key"]), **result.metrics})
    returns = [row["total_return"] for row in rows]
    profitable_ratio = sum(value > 0 for value in returns) / len(returns)
    stability = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    summary = {"median_return": statistics.median(returns),
               "profitable_window_ratio": profitable_ratio,
               "return_stability": stability,
               "worst_drawdown": min(row["max_drawdown"] for row in rows)}
    generator = random.Random(42)
    bootstrap = [statistics.mean(generator.choices(returns, k=len(returns))) for _ in range(1000)]
    bootstrap.sort()
    summary["bootstrap_return_p05"] = bootstrap[49]
    summary["bootstrap_positive_probability"] = sum(value > 0 for value in bootstrap) / len(bootstrap)
    sensitivity = []
    for factor in (1.0, 1.5, 2.0):
        result = run_backtest(ordered, strategy, initial_cash, quantity, commission,
                              slippage_bps * factor)
        sensitivity.append({"滑点倍数": factor, "总收益率": result.metrics["total_return"],
                            "最大回撤": result.metrics["max_drawdown"]})
    passed = (summary["median_return"] > 0 and profitable_ratio >= 0.5 and
              summary["worst_drawdown"] >= -0.30)
    return {
        "passed": passed,
        "summary": summary,
        "windows": rows,
        "sensitivity": sensitivity,
        "evidence": {
            "ordered": True,
            "bootstrap_seed": 42,
            "bootstrap_samples": len(bootstrap),
            "window_count": len(rows),
            "start": str(ordered.iloc[0]["time_key"]),
            "end": str(ordered.iloc[-1]["time_key"]),
        },
    }
