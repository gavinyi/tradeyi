"""AUC 门控守卫（P2 前置，design §4b）。

原缺陷精确形态：`load_auc` 在 pkl 没有 `metrics` 键时返回 **0.0**，于是
`new_auc < AUC_GUARD` 恒真 —— **自动重训永远不会替换生产模型**。
日志里只有一行"低于安全阈值"，没有告警、没人看日志，和"ML 静默降级 6 周"同一个形态。

本文件钉住四件事：
1. 读不到 AUC 必须是 `None`，**不能是 0.0**；
2. AUC 来源：json 的 `auc` → pkl 的 `metrics.auc` → pkl 顶层 `auc`；
3. 低于门控 → SKIP 且**不动生产文件**；
4. 改进幅度小于 `min_improvement_margin`（默认 0.01）→ SKIP，防止把噪声当改进。

变异：把 `load_auc` 末尾的 `return None` 改回 `return 0.0` → 第 1、4 条用例转红。
"""

from __future__ import annotations

import pickle

import pytest

from scripts.ml_retrain_monthly import (
    MIN_IMPROVEMENT_MARGIN,
    load_auc,
    promote_if_better,
)


def _write_pkl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        pickle.dump(payload, handle)
    return path


def test_load_auc_sources_and_missing_is_none(tmp_path):
    # pkl 的 metrics.auc
    nested = _write_pkl(tmp_path / "nested.pkl", {"metrics": {"auc": 0.61}, "model": object()})
    assert load_auc(nested) == pytest.approx(0.61)

    # pkl 顶层 auc
    flat = _write_pkl(tmp_path / "flat.pkl", {"auc": 0.72, "model": object()})
    assert load_auc(flat) == pytest.approx(0.72)

    # json 的 auc 优先
    with_json = _write_pkl(tmp_path / "withjson.pkl", {"model": object()})
    with_json.with_suffix(".json").write_text('{"auc": 0.83}', encoding="utf-8")
    assert load_auc(with_json) == pytest.approx(0.83)

    # 两者都没有 → None（**不是 0.0**：0.0 会让门控恒真、重训永远不生效）
    missing = _write_pkl(tmp_path / "missing.pkl", {"model": object()})
    assert load_auc(missing) is None


def test_promote_skips_when_auc_missing_and_keeps_production(tmp_path):
    production = _write_pkl(tmp_path / "prod.pkl", {"metrics": {"auc": 0.60}, "model": "old"})
    candidate = _write_pkl(tmp_path / "candidate.pkl", {"model": "new"})

    promoted, new_auc, old_auc = promote_if_better(candidate, production)

    assert promoted is False
    assert new_auc is None
    assert old_auc == pytest.approx(0.60)
    with open(production, "rb") as handle:
        assert pickle.load(handle)["model"] == "old", "SKIP 时绝不能动生产模型文件"


def test_promote_skips_below_guard(tmp_path):
    production = _write_pkl(tmp_path / "prod.pkl", {"metrics": {"auc": 0.40}, "model": "old"})
    candidate = _write_pkl(tmp_path / "candidate.pkl", {"metrics": {"auc": 0.45}, "model": "new"})

    promoted, _, _ = promote_if_better(candidate, production)

    assert promoted is False, "低于安全阈值的模型不得替换生产模型"


def test_promote_requires_min_improvement_margin(tmp_path):
    production = _write_pkl(tmp_path / "prod.pkl", {"metrics": {"auc": 0.60}, "model": "old"})
    # 只比当前好 0.005 < 默认边际 0.01 → 不得替换（样本少时 0.005 在噪声内）
    candidate = _write_pkl(
        tmp_path / "candidate.pkl",
        {"metrics": {"auc": 0.60 + MIN_IMPROVEMENT_MARGIN / 2}, "model": "new"},
    )

    promoted, _, _ = promote_if_better(candidate, production)

    assert promoted is False


def test_promote_replaces_when_clearly_better(tmp_path):
    production = _write_pkl(tmp_path / "prod.pkl", {"metrics": {"auc": 0.60}, "model": "old"})
    candidate = _write_pkl(tmp_path / "candidate.pkl", {"metrics": {"auc": 0.70}, "model": "new"})

    promoted, new_auc, old_auc = promote_if_better(candidate, production)

    assert promoted is True
    assert new_auc == pytest.approx(0.70) and old_auc == pytest.approx(0.60)
    with open(production, "rb") as handle:
        assert pickle.load(handle)["model"] == "new", "明确改进时应当替换生产模型"
