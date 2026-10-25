"""重导 CSV 的合约级列契约守卫（Chief/Reviewer 2026-10-25 要求）。

背景：P0-A 只改了导出脚本，产物从未重导——生产/测试的
`data/ml_training/volatility_trades_v3_latest.csv` 长期是 564 行 / 39 列旧版，
没有 `moneyness` / `gamma_to_theta`。后果：
- 比较器已 fail-closed → 大屏点按钮直接报错；
- Layer C 每个组都会「特征缺失 → 回退中立 tie-break」→ **看着上线实际没生效**（假生效）。

本守卫就是防「假生效」：断言 CSV 真含这三类列且有覆盖率门槛。
变异：把 `moneyness` / `gamma_to_theta` / `*_missing_reason` 任一列从 DataFrame 删掉
→ `_assert_contract` 必须抛 AssertionError。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
CSV_CANDIDATES = (
    ROOT / "data/ml_training/volatility_trades_v3_latest.csv",
    Path(r"E:\Thisyi\trade\data\ml_training\volatility_trades_v3_latest.csv"),
)

REQUIRED_COLUMNS = (
    "moneyness",
    "gamma_to_theta",
    "moneyness_missing_reason",
    "gamma_to_theta_missing_reason",
    "moneyness_lag_minutes",
    "days_to_expiry",
)
# 覆盖率门槛：P0-A 实测两列均 551/551；门槛放到 99% 以容忍边界，但低于门槛就是
# 「大范围缺特征 → Layer C 退化」必须先查导出，不许当成正常波动。
MIN_COVERAGE = 0.99
MAX_LAG_MINUTES = 45.0


def _csv_path() -> Path:
    """定位共享数据产物：优先本 worktree，其次生产目录（同一份共享数据契约）。

    ⚠️ 这份 CSV **不进版本库**（Chief 2026-10-25 裁定：`data/ml_training/` 已 ignore，
    5 份旧 CSV 只是历史误提交；`git rm --cached` 后仅留盘上文件）。所以守卫校验的是
    **共享数据契约**（盘上产物必须含合约级列），不校验「本分支产物」；缺失时**直接失败**
    并给出重导命令，不用 skip —— skip 会让「没重导」安静地通过，正是假生效。
    """
    for candidate in CSV_CANDIDATES:
        if candidate.exists():
            return candidate
    pytest.fail(
        "找不到 volatility_trades_v3_latest.csv（本 worktree 与生产目录都没有）。"
        "这是共享数据契约，请先重导（它不进版本库）：\n"
        "  python scripts/ml_export_training_data_v3.py "
        "--output data/ml_training/volatility_trades_v3_latest.csv --db data/trading.db\n"
        "生产目录 E:\\Thisyi\\trade\\data\\ml_training\\ 的副本同样可用。"
    )


def _assert_contract(frame: pd.DataFrame) -> None:
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    assert not missing, (
        f"回测 CSV 缺合约级列 {missing}：Layer C / 比较器会整组降级或直接报错"
        "（P0-A 后必须重导，不许拿旧 CSV 验收）"
    )
    for column in ("moneyness", "gamma_to_theta", "days_to_expiry"):
        coverage = float(frame[column].notna().mean())
        assert coverage >= MIN_COVERAGE, (
            f"{column} 覆盖率 {coverage:.2%} < {MIN_COVERAGE:.0%}，"
            "Layer C 会大面积退化成中立 tie-break（看着上线实际没生效）"
        )
    reasons = frame["moneyness_missing_reason"].dropna()
    assert not (reasons != "OK").any(), (
        f"moneyness 存在非 OK 缺失原因：{reasons[reasons != 'OK'].unique().tolist()}"
    )
    lag = frame["moneyness_lag_minutes"].dropna()
    assert (lag > MAX_LAG_MINUTES).sum() == 0, (
        f"有 {(lag > MAX_LAG_MINUTES).sum()} 条快照滞后 > {MAX_LAG_MINUTES:.0f} 分钟"
        "（导出口径应置空并落原因，不许悄悄漂）"
    )


def test_reexport_csv_has_contract_level_columns():
    frame = pd.read_csv(_csv_path())
    assert len(frame) >= 100, f"回测样本仅 {len(frame)} 行，样本不足"
    _assert_contract(frame)


@pytest.mark.parametrize(
    "dropped",
    ["moneyness", "gamma_to_theta", "moneyness_missing_reason",
     "gamma_to_theta_missing_reason", "moneyness_lag_minutes"],
)
def test_mutation_dropped_contract_column_must_be_red(dropped):
    """变异：删掉任一合约级列 → 契约守卫必须红（防「看着上线实际没生效」）。"""
    frame = pd.read_csv(_csv_path())
    mutated = frame.drop(columns=[dropped])
    with pytest.raises(AssertionError):
        _assert_contract(mutated)


def test_mutation_low_coverage_must_be_red():
    """变异：把 moneyness 大比例置空 → 覆盖率门槛必须红。"""
    frame = pd.read_csv(_csv_path())
    mutated = frame.copy()
    mutated.loc[mutated.index[: int(len(mutated) * 0.2)], "moneyness"] = float("nan")
    with pytest.raises(AssertionError):
        _assert_contract(mutated)
