"""重建 / 校验 ML 模型产物（pkl）与元数据（json）的配对（A7，2026-10-27）。

背景：`volatility_scorer_v3.json` 曾写成 `v3.1_retrained_20261024` / 7 个特征，
而同名 pkl 实际是 `v3` / 16 个特征（RandomForestClassifier，2026-08-29 训练）。
两文件不配对 → 任何按 json 解读模型的行为都是错的（回测对照、面板展示）。

口径：
- **pkl 是唯一真相**（运行时加载的是它）；
- json 只做它的元数据镜像：version / trained_at / features(=feature_cols) / model_type /
  feature_count；
- 本脚本两种模式：
    python scripts/rebuild_ml_model_metadata.py --check   # 只校验，不通过 exit 1
    python scripts/rebuild_ml_model_metadata.py --apply   # 用 pkl 元数据重建 json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import pickle
import sys


def load_pkl_metadata(pkl_path: Path) -> dict:
    """读取 pkl 里的模型元数据（只依赖 pickle 结构，不依赖 sklearn 调用）。"""
    with open(pkl_path, "rb") as handle:
        payload = pickle.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{pkl_path} 不是字典结构，无法提取元数据：{type(payload).__name__}")
    feature_cols = payload.get("feature_cols") or payload.get("features") or []
    model = payload.get("model")
    return {
        "artifact": pkl_path.name,
        "version": str(payload.get("version") or "unknown"),
        "trained_at": payload.get("trained_at"),
        "model_type": type(model).__name__ if model is not None else "unknown",
        "features": list(feature_cols),
        "feature_count": len(feature_cols),
    }


def metadata_mismatches(meta: dict, pkl_meta: dict) -> list[str]:
    """比较 json 元数据与 pkl 真相，返回差异清单（空 = 配对一致）。"""
    problems: list[str] = []
    expected_features = list(pkl_meta["features"])
    actual_features = list(meta.get("features") or [])
    if actual_features != expected_features:
        problems.append(
            f"features 不一致：json={len(actual_features)} 个 / pkl={len(expected_features)} 个"
        )
    json_version = str(meta.get("version") or "")
    # 版本必须**逐字一致**：用前缀判定会把 `v3.1_retrained_20261024` 误认为 pkl 的 `v3`
    # （2026-10-27 全量红就是这么来的：「跑的是 v3.1、pkl 是 v3」却报配对成功）。
    if json_version and json_version != str(pkl_meta["version"]):
        problems.append(f"version 不一致：json={json_version} / pkl={pkl_meta['version']}")
    json_trained = str(meta.get("trained_at") or "")
    pkl_trained = str(pkl_meta.get("trained_at") or "")
    if json_trained and pkl_trained and json_trained != pkl_trained:
        problems.append(f"trained_at 不一致：json={json_trained} / pkl={pkl_trained}")
    return problems


def build_metadata(pkl_meta: dict, previous: dict | None = None) -> dict:
    """由 pkl 元数据构造新的 json（保留不冲突的补充字段）。"""
    metadata = {
        "artifact": pkl_meta["artifact"],
        "version": pkl_meta["version"],
        "trained_at": pkl_meta.get("trained_at"),
        "model_type": pkl_meta["model_type"],
        "feature_count": pkl_meta["feature_count"],
        "features": list(pkl_meta["features"]),
        "source": "rebuilt_from_pkl",
    }
    for key, value in (previous or {}).items():
        if key not in metadata:
            metadata[key] = value
    return metadata


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重建/校验 ML 模型 pkl↔json 配对")
    parser.add_argument("--model", default="data/ml_models/volatility_scorer_v3.pkl")
    parser.add_argument("--apply", action="store_true", help="用 pkl 元数据重写 json")
    parser.add_argument("--check", action="store_true", help="只校验（默认行为）")
    args = parser.parse_args(argv)

    pkl_path = Path(args.model)
    if not pkl_path.exists():
        print(f"[ERROR] 模型文件不存在：{pkl_path}", file=sys.stderr)
        return 2
    json_path = pkl_path.with_suffix(".json")
    pkl_meta = load_pkl_metadata(pkl_path)
    previous: dict = {}
    if json_path.exists():
        try:
            previous = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
    problems = metadata_mismatches(previous, pkl_meta) if previous else ["json 缺失"]
    print(f"pkl: version={pkl_meta['version']} features={pkl_meta['feature_count']} "
          f"trained_at={pkl_meta.get('trained_at')}")
    if args.apply:
        rebuilt = build_metadata(pkl_meta, previous)
        json_path.write_text(
            json.dumps(rebuilt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(f"[OK] 已用 pkl 元数据重建 {json_path}")
        return 0
    if problems:
        for problem in problems:
            print(f"[MISMATCH] {problem}")
        print(f"共 {len(problems)} 处不配对；用 --apply 以 pkl 为真相重建 json")
        return 1
    print("[OK] pkl 与 json 配对一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
