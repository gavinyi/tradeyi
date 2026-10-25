"""重导 CSV 的合约级列契约守卫（Reviewer 2026-10-25 加固版）。

背景：P0-A 只改了导出脚本、产物从未重导 → 生产/测试 CSV 长期缺
`moneyness` / `gamma_to_theta`，导致比较器 fail-closed 报错、Layer C 在每组
「特征缺失 → 回退中立」→ **看着上线实际没生效**（假生效）。

加固口径（Reviewer 三条意见）：
1. **单测自给自足**：用 fixture 造小 CSV 验证列契约与变异，不依赖盘上 551 行产物；
2. **外部真实副本只经 `TRADEYI_ML_CSV` 显式注入**，测试里**不写死盘符/绝对路径**
   ——否则 worktree 里 CSV 陈旧、生产新鲜时，守卫会拿生产那份判绿，分支自己的产物没被验证；
3. **真实产物新鲜度/覆盖率** 属运维巡检项（Ops 侧），单测只做「显式注入时顺带校验」，
   未注入时 skip 并写明由 Ops 巡检负责。

变异（必须红）：删任一合约级列 / 覆盖率跌破门槛 / 缺失原因非 OK / lag 超 45 分钟。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV_CSV = "TRADEYI_ML_CSV"
REQUIRED_COLUMNS = (
    "moneyness",
    "gamma_to_theta",
    "moneyness_missing_reason",
    "gamma_to_theta_missing_reason",
    "moneyness_lag_minutes",
    "days_to_expiry",
)
# 覆盖率门槛：P0-A 实测两列均 551/551；门槛 99% 容忍边界，低于门槛说明导出退化。
MIN_COVERAGE = 0.99
MAX_LAG_MINUTES = 45.0
MIN_ROWS = 10
# 测试文件自身不得出现 Windows 盘符绝对路径（外部路径只能经 env 注入）。
# 必须同时吃 `/` 与 `\` 两种写法：本仓库大量用正斜杠路径（`as_posix()`、脚本里的盘符加斜杠），
# 只匹配反斜杠等于「名义上的守卫」（Reviewer 2026-10-25 变异证红）。
_DRIVE_PATH_PATTERN = re.compile(r"[A-Za-z]:[/\\]")

# 自检样本：源码里用字符串拼接构造，避免样本本身触发「源码含盘符」扫描。
_BAD_DRIVE_PATH_SAMPLES = (
    "E" + ":" + "/tmp/x.csv",
    "E" + ":" + "\\" + "tmp" + "\\" + "x.csv",
)


def _assert_contract(frame: pd.DataFrame) -> None:
    """列契约断言（单测与 env 注入的真实副本共用同一套口径）。"""
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


@pytest.fixture()
def contract_frame() -> pd.DataFrame:
    """自给小 CSV：12 行，含全部合约级列且取值合法（不依赖盘上真实产物）。"""
    rows = []
    for index in range(12):
        rows.append({
            "trade_date": f"2026-08-{index + 3:02d}",
            "underlying_symbol": "US.SPY",
            "strike_price": 400.0 + index,
            "is_call": 1,
            "expiry_bucket": 1,
            "days_to_expiry": 2 + index % 5,
            "moneyness": 0.01 * index,
            "gamma_to_theta": 1.5 + 0.1 * index,
            "moneyness_missing_reason": "OK",
            "gamma_to_theta_missing_reason": "OK",
            "moneyness_lag_minutes": 0.5,
            "window_net_pnl": 10.0 * index,
        })
    return pd.DataFrame(rows)


def _write_csv(frame: pd.DataFrame, path: Path) -> Path:
    frame.to_csv(path, index=False, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 单测（fixture）
def test_fixture_csv_passes_contract(contract_frame, tmp_path):
    path = _write_csv(contract_frame, tmp_path / "contract_ok.csv")
    _assert_contract(pd.read_csv(path))


@pytest.mark.parametrize("dropped", REQUIRED_COLUMNS)
def test_mutation_dropped_contract_column_must_be_red(contract_frame, tmp_path, dropped):
    mutated = contract_frame.drop(columns=[dropped])
    path = _write_csv(mutated, tmp_path / "dropped.csv")
    with pytest.raises(AssertionError):
        _assert_contract(pd.read_csv(path))


def test_mutation_low_coverage_must_be_red(contract_frame, tmp_path):
    mutated = contract_frame.copy()
    mutated.loc[mutated.index[: int(len(mutated) * 0.2)], "moneyness"] = float("nan")
    path = _write_csv(mutated, tmp_path / "low_coverage.csv")
    with pytest.raises(AssertionError):
        _assert_contract(pd.read_csv(path))


def test_mutation_non_ok_missing_reason_must_be_red(contract_frame, tmp_path):
    mutated = contract_frame.copy()
    mutated.loc[0, "moneyness_missing_reason"] = "snapshot_missing_or_stale"
    path = _write_csv(mutated, tmp_path / "bad_reason.csv")
    with pytest.raises(AssertionError):
        _assert_contract(pd.read_csv(path))


def test_mutation_lag_over_45_minutes_must_be_red(contract_frame, tmp_path):
    mutated = contract_frame.copy()
    mutated.loc[0, "moneyness_lag_minutes"] = MAX_LAG_MINUTES + 1.0
    path = _write_csv(mutated, tmp_path / "lag.csv")
    with pytest.raises(AssertionError):
        _assert_contract(pd.read_csv(path))


# ------------------------------------------------- 外部真实副本（仅 env 注入）
def test_env_injected_csv_contract():
    """只有显式设置 `TRADEYI_ML_CSV` 时才校验真实副本。

    真实产物的**新鲜度/覆盖率**是 Ops 巡检项，不塞进单测：worktree 里 CSV 可能陈旧，
    拿它判绿等于「分支产物没被验证」；拿生产路径兜底则会掩盖分支问题。
    """
    raw = os.environ.get(ENV_CSV, "").strip()
    if not raw:
        pytest.skip(
            f"未设置 {ENV_CSV}：真实 CSV 的新鲜度/覆盖率由 Ops 巡检负责；"
            "本地需要联调时设置该环境变量指向一份真实导出产物"
        )
    path = Path(raw)
    assert path.exists(), f"{ENV_CSV} 指向的文件不存在：{path}"
    frame = pd.read_csv(path)
    assert len(frame) >= MIN_ROWS, f"{path} 样本仅 {len(frame)} 行，不足 {MIN_ROWS}"
    _assert_contract(frame)


# ------------------------------------------------- 结构守卫：不写死盘符路径
def _assert_no_drive_path(text: str) -> None:
    hits = _DRIVE_PATH_PATTERN.findall(text)
    assert not hits, (
        f"文本出现盘符绝对路径 {hits}：外部路径只能经 {ENV_CSV} 注入"
        "（否则 worktree 陈旧、生产新鲜时守卫会假绿）"
    )


def test_drive_path_pattern_self_check():
    """自检：模式必须命中 `/` 与 `\\` 两种形态，否则守卫「绿得毫无意义」。"""
    for sample in _BAD_DRIVE_PATH_SAMPLES:
        assert _DRIVE_PATH_PATTERN.search(sample), (
            f"盘符正则漏掉形态 {sample!r}：正斜杠路径会绕过守卫"
        )
    # 正常相对路径不许误报
    assert not _DRIVE_PATH_PATTERN.search("data/ml_training/x.csv")
    assert not _DRIVE_PATH_PATTERN.search("as_posix(): data/ml/x")


@pytest.mark.parametrize("sample", _BAD_DRIVE_PATH_SAMPLES)
def test_mutation_hardcoded_drive_path_must_be_red(sample):
    """变异：把任一斜杠形态的盘符路径塞进文本 → 结构守卫必须红。"""
    with pytest.raises(AssertionError):
        _assert_no_drive_path(f'path = Path("{sample}")')


def test_source_has_no_hardcoded_drive_path():
    _assert_no_drive_path(Path(__file__).read_text(encoding="utf-8"))


def test_env_var_documented_for_ops_patrol():
    """运维巡检口径必须写在测试里，避免以后又把新鲜度塞回单测。"""
    source = Path(__file__).read_text(encoding="utf-8")
    assert ENV_CSV in source
    assert "Ops 巡检" in source
