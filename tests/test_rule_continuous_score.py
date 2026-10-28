"""rule scorer v2（连续化）验收守卫 A1~A10。

设计：`data/qa/rule_continuous_score_design.md`（Chief 已签字）。
被修缺陷：旧 `BuiltinRuleModel` 只输出 11 个离散值 → 18 天里 11 天当日分数全并列 →
`pd.qcut` 退化成按行序硬切，rule 对照臂的五档/截面 IC 全测不出（对照组失效）。

变异（必须转红）：
- 把 `predict_proba` 改回旧离散公式 → A1（并列天数比）与 A2 必红；
- 去掉 L2 破并列 → 同日 Top3 并列天数回升，A1 红；
- 改任一冻结权重（`_RULE_W_*`）→ `test_weights_are_frozen` 必红。
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from futu_quant.ml_scorer import (  # noqa: E402
    BuiltinRuleModel,
    INFERENCE_FEATURE_KEYS,
    _RULE_TIE_STEP,
    _RULE_W_MOM,
    _RULE_W_PAYOFF,
    _RULE_W_PNL,
    _RULE_W_SHRINK,
    _RULE_W_TREND,
    _RULE_W_WIN,
    _continuous_components,
    load_ml_model,
)
CSV_PATH = ROOT / "data/ml_training/volatility_trades_v3_latest.csv"

FEATURE_COLS = [
    "symbol_win_rate", "symbol_avg_pnl", "symbol_pnl_trend", "symbol_std_pnl",
    "symbol_max_pnl", "symbol_min_pnl", "symbol_3d_avg_pnl", "symbol_7d_avg_pnl",
    "symbol_trade_count", "days_to_expiry", "expiry_bucket", "is_call", "strike_price",
]


def _load_frame() -> pd.DataFrame:
    if not CSV_PATH.exists():
        pytest.skip(f"缺少训练样本 {CSV_PATH}（历史 CSV 不在本 worktree）")
    frame = pd.read_csv(CSV_PATH)
    frame["trade_date"] = frame["trade_date"].astype(str)
    return frame.dropna(subset=["trade_date"])


def _old_rule_score(row) -> float:
    """旧实现（离散版）—— 只用于 A4/A5/A6 的「旧序」对照，不参与生产。

    缺字段/NaN 按旧实现的默认语义处理（旧实现直接 float(...) 会抛，这里取中性值），
    否则 NaN 参与比较会把「无法比较」误算成「方向翻转」。
    """

    def _num(key: str, default: float) -> float:
        value = row.get(key, default)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return default if number != number else number

    win_rate = _num("symbol_win_rate", 0.50)
    pnl_trend = _num("symbol_pnl_trend", 0.0)
    avg_pnl = _num("symbol_avg_pnl", 0.0)
    score = 0.50 + (win_rate - 0.50) * 0.35
    score += 0.05 if pnl_trend > 0 else (-0.05 if pnl_trend < 0 else 0.0)
    if avg_pnl > 0:
        score += 0.05
    return max(0.05, min(0.95, score))


def _new_scores(frame: pd.DataFrame) -> np.ndarray:
    return BuiltinRuleModel().predict_proba(frame[FEATURE_COLS])[:, 1]


def _top3(scores: np.ndarray) -> list[int]:
    return list(np.argsort(-scores, kind="stable")[:3])


def _tied_top3(scores: np.ndarray) -> bool:
    top = np.sort(scores)[::-1][:3]
    return bool(np.ptp(top) < 1e-12)


# ---------------------------------------------------------------- A1 / A2
def test_A1_same_day_top3_tie_ratio_below_5pct():
    frame = _load_frame()
    tied_days = 0
    days = 0
    for _, group in frame.groupby("trade_date"):
        if len(group) < 3:
            continue
        days += 1
        tied_days += int(_tied_top3(_new_scores(group)))
    assert days > 0
    assert tied_days / days < 0.05, (
        f"同日 Top3 并列天数 {tied_days}/{days} = {tied_days/days:.1%}，未达 <5%（旧实现 61%）"
    )


def test_A2_distinct_scores_per_day():
    frame = _load_frame()
    good = total = 0
    for _, group in frame.groupby("trade_date"):
        total += 1
        distinct = len(set(np.round(_new_scores(group), 12)))
        if distinct >= min(3, len(group)):
            good += 1
    assert good / total >= 0.95, f"distinct≥min(3,n) 的交易日占比 {good}/{total}"


# ---------------------------------------------------------------- A3 契约
def test_A3_output_contract():
    frame = _load_frame()
    probs = BuiltinRuleModel().predict_proba(frame[FEATURE_COLS])
    assert isinstance(probs, np.ndarray)
    assert probs.shape == (len(frame), 2)
    assert float(probs[:, 1].min()) >= 0.05
    assert float(probs[:, 1].max()) <= 0.95
    np.testing.assert_allclose(probs[:, 0], 1.0 - probs[:, 1])


# ---------------------------------------------------------------- A4 / A5 / A6
def test_A4_order_consistency_vs_old_at_least_99pct():
    """旧实现能分出序的样本对上，新实现序一致率 ≥99%（掉线即判定「变向」）。"""
    frame = _load_frame()
    agree = compared = 0
    for _, group in frame.groupby("trade_date"):
        if len(group) < 2:
            continue
        new = _new_scores(group)
        old = group.apply(_old_rule_score, axis=1).to_numpy()
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if abs(old[i] - old[j]) < 1e-12:
                    continue
                compared += 1
                agree += int(np.sign(new[i] - new[j]) == np.sign(old[i] - old[j]))
    assert compared > 0
    assert agree / compared >= 0.99, (
        f"新旧序一致率 {agree}/{compared} = {agree/compared:.2%} 低于 99% → 判「变向」"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A5 作废（Architect v1.1）：旧实现同标的合约分数恒等，旧 Top3 合约身份本就由 CSV 行序"
        "任意决定，重合率测的是「任意旧序 vs 确定新序」而非变向。实测合约级 Jaccard=0.503、"
        "按标的口径 0.706。strict=True：若将来意外通过，说明口径变了，必须回来看设计。"
    ),
)
def test_A5_top3_jaccard_mean_at_least_080():
    frame = _load_frame()
    scores_j = []
    for _, group in frame.groupby("trade_date"):
        if len(group) < 3:
            continue
        old = group.apply(_old_rule_score, axis=1).to_numpy()
        new = _new_scores(group)
        a, b = set(_top3(old)), set(_top3(new))
        scores_j.append(len(a & b) / len(a | b))
    assert np.mean(scores_j) >= 0.80, f"Top3 Jaccard 均值 {np.mean(scores_j):.3f} < 0.80"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A6 作废（Architect v1.1）：旧分在同标的内完全并列（average rank），破并列后组内序必然重排，"
        "秩相关被组内任意序拉低（实测 0.646）。方向一致性看 A4（99.38%）。"
        "strict=True：意外通过即说明口径变了，必须回看设计。"
    ),
)
def test_A6_daily_spearman_mean_at_least_090():
    frame = _load_frame()
    rhos = []
    for _, group in frame.groupby("trade_date"):
        if len(group) < 3:
            continue
        old = pd.Series(group.apply(_old_rule_score, axis=1).to_numpy())
        new = pd.Series(_new_scores(group))
        if old.nunique() < 2 or new.nunique() < 2:
            continue
        rhos.append(old.rank().corr(new.rank()))
    assert np.mean(rhos) >= 0.90, f"日均 Spearman {np.mean(rhos):.3f} < 0.90"


# ---------------------------------------------------------------- A7 五档可分
def test_A7_qcut_does_not_degenerate():
    frame = _load_frame()
    ok = total = 0
    for _, group in frame.groupby("trade_date"):
        if len(group) < 5:
            continue
        total += 1
        scores = pd.Series(_new_scores(group))
        if scores.nunique() < 5:
            continue
        bins = pd.qcut(scores, 5, labels=False, duplicates="drop")
        ok += int(pd.Series(bins).nunique() == 5)
    assert total > 0
    assert ok / total >= 0.95, f"qcut 不退化交易日 {ok}/{total}"


# ---------------------------------------------------------------- A8 无 sklearn
def test_A8_no_sklearn_dependency():
    code = (
        "import sys; sys.path.insert(0, 'src');"
        "import pandas as pd;"
        "from futu_quant.ml_scorer import BuiltinRuleModel;"
        "frame = pd.DataFrame([{'symbol_win_rate': 0.6, 'symbol_avg_pnl': 10.0,"
        " 'symbol_std_pnl': 20.0, 'symbol_trade_count': 3}]);"
        "probs = BuiltinRuleModel().predict_proba(frame);"
        "assert probs.shape == (1, 2);"
        "assert 'sklearn' not in sys.modules, 'rule scorer 不应导入 sklearn';"
        "print('RULE_OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=ROOT, timeout=120, encoding="utf-8", errors="replace",
        # 子进程要 import 本仓包，必须显式给 PYTHONPATH（不依赖 venv 的 editable .pth）
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert "RULE_OK" in (result.stdout or ""), result.stderr[-800:]


# ---------------------------------------------------------------- A9 / A10 兜底路径
def _assert_fallback_guard(model, frame: pd.DataFrame) -> None:
    assert isinstance(model, BuiltinRuleModel), "兜底没有真的发生"
    scores = model.predict_proba(frame[FEATURE_COLS])[:, 1]
    # G1：连续 → 同日候选池 distinct 分数个数 ≥ min(3, n)
    assert len(set(np.round(scores, 12))) >= min(3, len(scores)), "兜底打分仍离散/并列"
    # G2：区间与形状不变
    assert float(scores.min()) >= 0.05 and float(scores.max()) <= 0.95
    # G3：同日 Top3 不并列
    assert not _tied_top3(scores), "兜底后同日 Top3 仍并列"


def _busiest_day(min_rows: int = 8) -> pd.DataFrame:
    frame = _load_frame()
    best = max(
        (group for _, group in frame.groupby("trade_date")),
        key=len,
    )
    assert len(best) >= min_rows, f"最忙交易日仅 {len(best)} 行，样本不足"
    return best


def test_A9a_fallback_by_signature_reject(tmp_path, caplog):
    path = tmp_path / "bad_features.pkl"
    with open(path, "wb") as handle:
        pickle.dump({"model": object(), "feature_cols": ["days_to_expiry", "bogus_feature"]}, handle)
    with caplog.at_level("WARNING"):
        model, _ = load_ml_model(str(path))
    _assert_fallback_guard(model, _busiest_day())
    assert "rule scorer v2" in caplog.text  # A10


def test_A9b_fallback_by_corrupt_pickle(tmp_path, caplog):
    path = tmp_path / "corrupt.pkl"
    path.write_bytes(b"not-a-pickle\x00\x01")
    with caplog.at_level("WARNING"):
        model, _ = load_ml_model(str(path))
    _assert_fallback_guard(model, _busiest_day())
    assert "rule scorer v2" in caplog.text  # A10


def test_A9c_fallback_by_missing_file(tmp_path, caplog):
    path = tmp_path / "does_not_exist.pkl"
    with caplog.at_level("WARNING"):
        model, _ = load_ml_model(str(path))
    _assert_fallback_guard(model, _busiest_day())
    assert "rule scorer v2" in caplog.text  # A10


# ---------------------------------------------------------------- 冻结常量
def test_weights_are_frozen():
    """改权重/步长必须红：对照组口径不允许运行时漂移。"""
    assert (_RULE_W_WIN, _RULE_W_TREND, _RULE_W_PNL) == (0.175, 0.05, 0.05)
    assert (_RULE_W_MOM, _RULE_W_PAYOFF, _RULE_W_SHRINK) == (0.020, 0.010, 0.005)
    assert _RULE_TIE_STEP == 1e-9


def test_continuous_components_missing_fields_do_not_raise():
    """缺字段容错：只有空 Series 也不许抛异常，且输出有界。"""
    components = _continuous_components(pd.Series(dtype=float))
    assert 0.05 <= min(0.95, max(0.05, components["raw"])) <= 0.95


def test_all_rule_inputs_are_in_inference_contract():
    """rule 打分只用推理侧可产字段（否则兜底在实盘会拿不到数据）。"""
    for column in ("symbol_win_rate", "symbol_avg_pnl", "symbol_std_pnl", "symbol_pnl_trend"):
        assert column in INFERENCE_FEATURE_KEYS


# ---------------------------------------------------------------- Layer B 作用域
def test_layer_b_only_applies_inside_a_tied_groups():
    """守卫（Reviewer #2）：A 层非全等分的日子，B 层不得给**唯一 A 值**的行加任何贡献。

    设计 §1.2 修正版：B 只在 A 完全等分时启用。若谁改回「始终相加」，
    唯一 A 值的行会带上 score_b → 本断言立即红。
    """
    frame = _load_frame()
    checked_days = 0
    for _, group in frame.groupby("trade_date"):
        if len(group) < 3:
            continue
        rows = list(group.iterrows())
        components = [_continuous_components(row) for _, row in rows]
        a_values = [item["score_a"] for item in components]
        if len(set(a_values)) < 2:  # 当日 A 层全等分 → 属于 B 的正常作用域，跳过
            continue
        checked_days += 1
        scores = _new_scores(group)
        for index, value in enumerate(a_values):
            if a_values.count(value) == 1:
                # 唯一 A 值：允许有 L2 的 1e-9 级偏移，但不允许 B 的量级（≥1e-6）
                assert abs(scores[index] - value) < 1e-6, (
                    f"A 值唯一的行被加了 Layer B（{scores[index]:.9f} vs A={value:.9f}）"
                    "→ B 语义被改回「始终相加」"
                )
    assert checked_days > 0, "没有可检验的交易日"


# ---------------------------------------------------------------- A11（Architect v1.1）
def test_A11a_old_tied_groups_get_pairwise_distinct_new_scores():
    """A11①：旧并列组内的样本，新分必须两两不同（L2 真破了并列）。"""
    frame = _load_frame()
    checked = 0
    for _, group in frame.groupby("trade_date"):
        if len(group) < 3:
            continue
        old = group.apply(_old_rule_score, axis=1).to_numpy()
        new = _new_scores(group)
        groups: dict[float, list[int]] = {}
        for index, value in enumerate(old):
            groups.setdefault(value, []).append(index)
        for indices in groups.values():
            if len(indices) < 2:
                continue
            checked += 1
            values = {round(float(new[i]), 12) for i in indices}
            assert len(values) == len(indices), (
                f"旧并列组（{len(indices)} 行）新分仍有重复：{sorted(values)[:5]}"
            )
    assert checked > 0, "没有旧并列组可检验"


def test_A11b_flipped_pairs_only_from_old_tied_pairs():
    """A11②：逆序对（旧能分辨、新反了）占比 ≤1%，即 A4 残差带。"""
    frame = _load_frame()
    flipped = compared = 0
    for _, group in frame.groupby("trade_date"):
        if len(group) < 2:
            continue
        old = group.apply(_old_rule_score, axis=1).to_numpy()
        new = _new_scores(group)
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if abs(old[i] - old[j]) < 1e-12:
                    continue
                compared += 1
                if np.sign(new[i] - new[j]) != np.sign(old[i] - old[j]):
                    flipped += 1
    assert compared > 0
    assert flipped / compared <= 0.01, f"逆序对 {flipped}/{compared} 超过 1%"


# ---------------------------------------------------------------- 旧序基准核对
def _load_baseline_model_class():
    """从 `fd95b00` 载入**真实旧实现**，避免 A4 的「旧序」是测试里重写的近似公式。"""
    result = subprocess.run(
        ["git", "show", "fd95b00:src/futu_quant/ml_scorer.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        # 必须显式 UTF-8：源码含中文注释，默认按 locale（本机 GBK）解码会在
        # 非 UTF-8 环境抛 UnicodeDecodeError → stdout 为空 → 假红/假绿（2026-10-25 Reviewer 实测）
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        pytest.skip(f"无法从 git 读取基线实现：{result.stderr[-200:]}")
    module = types.ModuleType("baseline_ml_scorer")
    exec(compile(result.stdout, "<fd95b00:ml_scorer>", "exec"), module.__dict__)  # noqa: S102
    return module.BuiltinRuleModel


def test_old_rule_score_matches_actual_baseline_fd95b00():
    """A4 的前提：`_old_rule_score` 与基线 `fd95b00` 的真 `BuiltinRuleModel` 逐值相等。"""
    baseline = _load_baseline_model_class()
    frame = _load_frame()
    baseline_scores = baseline().predict_proba(frame[FEATURE_COLS])[:, 1]
    expected = frame.apply(_old_rule_score, axis=1).to_numpy()
    np.testing.assert_allclose(baseline_scores, expected, rtol=0, atol=1e-12)
