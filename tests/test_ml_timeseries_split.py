"""时间序（walk-forward）训练切分守卫。

背景：旧 `ml_train_symbol_scorer_v3.py` 用
`train_test_split(random_state=42, stratify=y)` 随机切分 → **同一天的样本会同时进训练集和
测试集**，模型提前见过答案，切模拟盘测出来的 forward 证据不可用（2026-10-25 裁定根因）。

本守卫：
1. 切分必须按 `trade_date` 时间序、同日样本绝不跨集；
2. 反泄漏断言（训练最晚日 < 验证最早日 < 测试最早日）**必须抛错**而不是 warning；
3. 变异：把旧的随机切分索引传进去 → 必须红；
4. 默认 `--split-mode timeseries`，`random` 仅保留作对比复现；
5. 端到端：小 CSV 跑一次训练，产出的模型 metrics 必须带 `split_mode` / `rank_ic` / 日期跨度。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for extra in (str(ROOT), str(SCRIPTS)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ml_train_symbol_scorer_v3 as trainer  # noqa: E402

TRAIN_SCRIPT = SCRIPTS / "ml_train_symbol_scorer_v3.py"


def _synthetic_frame(days: int = 40, per_day: int = 3) -> pd.DataFrame:
    rows = []
    for day in range(days):
        trade_date = (
            f"2026-07-{day + 1:02d}" if day < 28 else f"2026-08-{day - 27:02d}"
        )
        for offset in range(per_day):
            index = day * per_day + offset
            rows.append({
                "trade_date": trade_date,
                "is_profitable": int(index % 2 == 0),
                "window_net_pnl": float(index % 7) - 3.0,
                "forward_days": 7,
                "underlying_symbol": "US.SPY",
            })
    return pd.DataFrame(rows)


# ------------------------------------------------------------- 切分语义
def test_timeseries_split_keeps_days_whole_and_ordered():
    frame = _synthetic_frame()
    train_idx, valid_idx, test_idx = trainer.time_series_split_indices(
        frame, test_size=0.2, valid_size=0.15
    )
    dates = frame["trade_date"].astype(str)

    train_days = set(dates.iloc[train_idx])
    valid_days = set(dates.iloc[valid_idx])
    test_days = set(dates.iloc[test_idx])

    # 同日样本不跨集
    assert not train_days & valid_days
    assert not train_days & test_days
    assert not valid_days & test_days
    # 三段覆盖全部行且互不重叠
    assert len(set(train_idx) | set(valid_idx) | set(test_idx)) == len(frame)
    assert len(train_idx) + len(valid_idx) + len(test_idx) == len(frame)
    # 时间序严格递增
    assert max(train_days) < min(valid_days) < max(valid_days) < min(test_days)


def test_leakage_assertion_passes_and_returns_spans():
    frame = _synthetic_frame()
    train_idx, valid_idx, test_idx = trainer.time_series_split_indices(frame)
    dates = frame["trade_date"].astype(str).reset_index(drop=True)
    spans = trainer.assert_no_temporal_leakage(dates, train_idx, valid_idx, test_idx)
    assert spans["train"][1] < spans["valid"][0] < spans["test"][0]


def test_mutation_random_split_must_raise():
    """变异：旧的随机切分索引 → 反泄漏断言必须抛错（证明旧口径现在是红的）。"""
    frame = _synthetic_frame()
    from sklearn.model_selection import train_test_split

    indices = np.arange(len(frame))
    train_idx, test_idx = train_test_split(indices, test_size=0.2, random_state=42)
    train_idx, valid_idx = train_test_split(train_idx, test_size=0.15, random_state=42)
    dates = frame["trade_date"].astype(str).reset_index(drop=True)
    with pytest.raises(RuntimeError, match="时序泄漏"):
        trainer.assert_no_temporal_leakage(dates, train_idx, valid_idx, test_idx)


def test_missing_trade_date_must_raise():
    frame = _synthetic_frame().drop(columns=["trade_date"])
    with pytest.raises(RuntimeError, match="trade_date"):
        trainer.time_series_split_indices(frame)


def test_too_few_days_must_raise():
    frame = _synthetic_frame(days=4, per_day=2)
    with pytest.raises(RuntimeError, match="交易日"):
        trainer.time_series_split_indices(frame)


def test_oversized_test_size_must_raise():
    frame = _synthetic_frame(days=10, per_day=1)
    with pytest.raises(RuntimeError, match="参数不合理"):
        trainer.time_series_split_indices(frame, test_size=0.5, valid_size=0.5)


def test_single_label_train_must_raise():
    frame = _synthetic_frame()
    dates = sorted(frame["trade_date"].unique())
    # 前 80% 的交易日全标成 0 → 训练段（最早 65% 交易日）必然是单标签
    train_days = set(dates[: int(len(dates) * 0.8)])
    frame.loc[frame["trade_date"].isin(train_days), "is_profitable"] = 0
    with pytest.raises(RuntimeError, match="单一标签"):
        trainer.time_series_split_indices(frame)


# ------------------------------------------------------------- 默认口径守卫
def test_default_split_mode_is_timeseries_and_leak_guard_is_wired():
    source = TRAIN_SCRIPT.read_text(encoding="utf-8")
    assert "'--split-mode'" in source
    assert "default='timeseries'" in source
    assert "time_series_split_indices(" in source
    assert "assert_no_temporal_leakage(" in source
    # 随机切分只允许出现在 random 对比分支里
    assert "随机切分模式（仅用于与旧口径对比" in source


# ------------------------------------------------------------------ 端到端
def _training_csv(tmp_path: Path) -> Path:
    rows = []
    days = 40
    for day in range(days):
        for offset in range(3):
            rows.append({
                "trade_date": f"2026-08-{day + 1:02d}",
                "is_profitable": int((day + offset) % 2 == 0),
                "window_net_pnl": float((day * 3 + offset) % 9) - 4.0,
                "forward_days": 7,
                "underlying_symbol": "US.SPY",
                "strike_price": 400.0 + offset,
                "underlying_price": 400.0 + offset * 0.5,
                "option_gamma": 0.02 + 0.001 * offset,
                "option_theta": -0.05 - 0.001 * offset,
                "symbol_win_rate": 0.4 + 0.01 * offset,
                "symbol_avg_pnl": 1.0 + offset,
                "symbol_std_pnl": 10.0,
                "symbol_pnl_trend": 0.5 * offset,
                "symbol_max_pnl": 20.0,
                "symbol_min_pnl": -8.0,
                "symbol_trade_count": 5 + offset,
                "symbol_3d_avg_pnl": 0.5,
                "symbol_7d_avg_pnl": 0.3,
                "is_call": 1,
                "days_to_expiry": 5 + offset,
                "expiry_bucket": 1,
                "moneyness": 0.01 * offset,
                "gamma_to_theta": 0.4 + 0.01 * offset,
            })
    path = tmp_path / "training.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    return path


def _run_training(csv_path: Path, model_path: Path, *, extra_env: dict | None = None,
                  split_mode: str = "timeseries") -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(TRAIN_SCRIPT), "--data", str(csv_path),
         "--model-output", str(model_path), "--split-mode", split_mode,
         "--model-type", "rf"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=ROOT, timeout=600, env=env,
    )


def test_end_to_end_training_records_split_metadata(tmp_path):
    path = _training_csv(tmp_path)
    model_path = tmp_path / "candidate.pkl"
    result = _run_training(path, model_path)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "时序切分" in result.stdout
    # 模型 pkl + 指标 sidecar json（save_model 的口径）
    assert model_path.exists()
    metrics_path = model_path.with_suffix(".json")
    assert metrics_path.exists(), "训练脚本必须落指标 sidecar（含切分口径与 IC）"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["split_mode"] == "timeseries"
    assert "rank_ic" in metrics
    span = metrics["split_date_span"]
    assert span["train"][1] < span["valid"][0] < span["test"][0], "模型 metrics 未记录时序跨度"
    assert json.dumps(span)  # 可序列化，便于报告与审计


def test_training_script_survives_gbk_stdout(tmp_path):
    """回归守卫（今日第 4 次同类编码事故）：GBK 控制台下训练脚本不得崩。

    手工重训是用户双击/命令行入口，Windows 默认 GBK；脚本里的 emoji print
    必须走 `_print_safe`，否则 `UnicodeEncodeError: 'gbk' codec can't encode`。
    """
    path = _training_csv(tmp_path)
    model_path = tmp_path / "gbk.pkl"
    result = _run_training(
        path, model_path,
        extra_env={"PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0"},
    )
    assert result.returncode == 0, (
        f"GBK 控制台下训练脚本崩溃（第 4 次同类编码事故）：\n{result.stderr[-1500:]}"
    )
    assert "UnicodeEncodeError" not in result.stderr
    assert model_path.exists()


def test_training_script_uses_safe_print():
    """源码守卫：emoji 输出必须走 `_print_safe`，不许裸 print 进 GBK 流。"""
    source = TRAIN_SCRIPT.read_text(encoding="utf-8")
    import re

    raw_prints = re.findall(r"(?<![.\w])print\(", source)
    # 只允许 `_print_safe` 定义体内的那 2 处 print
    assert len(raw_prints) <= 2, (
        f"训练脚本还有 {len(raw_prints)} 处裸 print：GBK 控制台下 emoji 会崩，改用 _print_safe"
    )
    assert "_print_safe(" in source
