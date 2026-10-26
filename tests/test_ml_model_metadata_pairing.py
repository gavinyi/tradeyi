"""A7 守卫：ML 模型 pkl ↔ json 元数据必须配对（2026-10-27）。

缺陷形态：`volatility_scorer_v3.json` 写成 `v3.1_retrained_20261024` / 7 特征，
同名 pkl 实际是 `v3` / 16 特征（2026-08-29 训练）。按 json 解读模型即错。

口径：**pkl 是唯一真相**，json 只是它的元数据镜像。
"""

from __future__ import annotations

import json
import pickle

import pytest

from scripts.rebuild_ml_model_metadata import (
    build_metadata,
    load_pkl_metadata,
    metadata_mismatches,
)


def _write_pkl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        pickle.dump(payload, handle)
    return path


def test_load_pkl_metadata_reads_truth(tmp_path):
    pkl = _write_pkl(tmp_path / "m.pkl", {
        "version": "v3",
        "trained_at": "2026-08-29T08:22:01",
        "feature_cols": ["a", "b", "c"],
        "model": object(),
    })
    meta = load_pkl_metadata(pkl)
    assert meta["version"] == "v3"
    assert meta["feature_count"] == 3
    assert meta["features"] == ["a", "b", "c"]
    assert meta["artifact"] == "m.pkl"


def test_mismatched_features_and_version_are_reported(tmp_path):
    pkl = _write_pkl(tmp_path / "m.pkl", {
        "version": "v3", "trained_at": "2026-08-29T08:22:01",
        "feature_cols": [f"f{i}" for i in range(16)], "model": object(),
    })
    meta = load_pkl_metadata(pkl)
    stale_json = {
        "version": "v3.1_retrained_20261024",
        "trained_at": "2026-10-24T10:36:54",
        "features": [f"x{i}" for i in range(7)],
    }
    problems = metadata_mismatches(stale_json, meta)
    assert any("features" in item for item in problems)
    assert any("version" in item for item in problems)
    assert any("trained_at" in item for item in problems)


def test_rebuild_uses_pkl_and_keeps_extra_fields(tmp_path):
    pkl = _write_pkl(tmp_path / "m.pkl", {
        "version": "v3", "trained_at": "2026-08-29T08:22:01",
        "feature_cols": ["a", "b"], "model": object(),
    })
    meta = load_pkl_metadata(pkl)
    rebuilt = build_metadata(meta, previous={
        "version": "v3.1_wrong", "features": ["x"], "sample_trades_count": 330,
    })
    assert rebuilt["version"] == "v3"
    assert rebuilt["features"] == ["a", "b"]
    assert rebuilt["feature_count"] == 2
    assert rebuilt["source"] == "rebuilt_from_pkl"
    # 不冲突的补充字段保留
    assert rebuilt["sample_trades_count"] == 330
    # 重建后必须配对
    assert metadata_mismatches(rebuilt, meta) == []


def test_rebuilt_json_roundtrip_matches_pkl(tmp_path):
    pkl = _write_pkl(tmp_path / "m.pkl", {
        "version": "v3", "trained_at": "2026-08-29T08:22:01",
        "feature_cols": ["a", "b", "c"], "model": object(),
    })
    meta = load_pkl_metadata(pkl)
    json_path = pkl.with_suffix(".json")
    json_path.write_text(
        json.dumps(build_metadata(meta, None), ensure_ascii=False), encoding="utf-8",
    )
    on_disk = json.loads(json_path.read_text(encoding="utf-8"))
    assert metadata_mismatches(on_disk, meta) == []


def test_non_dict_pickle_is_rejected(tmp_path):
    pkl = _write_pkl(tmp_path / "bad.pkl", [1, 2, 3])
    with pytest.raises(ValueError, match="不是字典结构"):
        load_pkl_metadata(pkl)
