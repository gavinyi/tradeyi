#!/usr/bin/env python3
"""
月度自动重训练脚本（供定时任务调用，无人值守）

流程：
1. 导出最新训练数据（波动率策略全部历史）
2. 训练新模型（v3 特征工程，自动选择 RandomForest/LightGBM）
3. 与当前生产模型对比 AUC
4. 若新模型 AUC >= 当前模型，备份旧模型并替换生产模型文件
5. 输出结构化报告（JSON + 控制台）

用法：
    uv run python scripts/ml_retrain_monthly.py
    uv run python scripts/ml_retrain_monthly.py --model-output data/ml_models/volatility_scorer_v3.pkl
"""

from __future__ import annotations

import argparse
import os
import json
import pickle
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

if sys.platform == "win32":
    import io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data/ml_training/volatility_trades_v3_latest.csv"
DEFAULT_MODEL = ROOT / "data/ml_models/volatility_scorer_v3.pkl"
BACKUP_DIR = ROOT / "data/ml_models/backups"
MIN_SAMPLES = 200  # 样本太少不训练
# AUC 门控默认值不变（design §3.3），但允许运维用 env 覆盖，便于按租户调。
AUC_GUARD = float(os.getenv("ML_AUC_GUARD", "0.50"))   # AUC 低于此值不替换生产模型（防退化）
# 相对改进边际：默认 0.01（design §2.1），样本少时防止把噪声当改进。
MIN_IMPROVEMENT_MARGIN = float(os.getenv("ML_MIN_IMPROVEMENT_MARGIN", "0.01"))


def run(cmd: list[str]) -> str:
    """运行子命令并返回输出"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"命令失败: {' '.join(cmd)}\nstdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )
    return result.stdout


def export_data(start_date: str | None = None) -> Path:
    """导出最新训练数据"""
    print("=" * 60)
    print("[EXPORT] [1/4] 导出训练数据")
    print("=" * 60)
    cmd = [
        sys.executable, str(ROOT / "scripts/ml_export_training_data_v3.py"),
        "--output", str(DEFAULT_DATA),
    ]
    if start_date:
        cmd += ["--start-date", start_date]
    output = run(cmd)
    print(output)
    if not DEFAULT_DATA.exists():
        raise RuntimeError("导出失败：数据文件未生成")
    return DEFAULT_DATA


def train_model(data_path: Path, model_output: Path) -> str:
    """训练新模型"""
    print("=" * 60)
    print("[TRAIN] [2/4] 训练新模型")
    print("=" * 60)
    cmd = [
        sys.executable, str(ROOT / "scripts/ml_train_symbol_scorer_v3.py"),
        "--data", str(data_path),
        "--model-output", str(model_output),
        "--model-type", "auto",
    ]
    output = run(cmd)
    print(output)
    return output


def load_metrics(model_path: Path) -> dict | None:
    """读取模型指标文件"""
    metrics_path = model_path.with_suffix(".json")
    if not metrics_path.exists():
        return None
    try:
        with open(metrics_path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def load_auc(model_path: Path) -> float | None:
    """读取模型 AUC；**读不到必须是 None，不能是 0.0**（design §4b）。

    原实现 `float(data.get("metrics", {}).get("auc", 0))`：pkl 里没有 metrics 键时
    返回 **0.0**，于是 `new_auc < AUC_GUARD` 恒真 —— **自动重训永远不会替换生产模型**
    （静默失效，日志里只有一行"低于安全阈值"，没人会看到）。
    """
    metrics = load_metrics(model_path)
    if metrics:
        for key in ("auc", "auc_score"):
            if key in metrics:
                try:
                    return float(metrics[key])
                except (TypeError, ValueError):
                    return None
    try:
        with open(model_path, "rb") as f:
            data = pickle.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    nested = data.get("metrics")
    if isinstance(nested, dict):
        for key in ("auc", "auc_score"):
            if key in nested:
                try:
                    return float(nested[key])
                except (TypeError, ValueError):
                    return None
    for key in ("auc", "auc_score"):
        if key in data:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                return None
    # 两种来源都没有 → None（不是 0.0）
    return None


def promote_if_better(new_model: Path, production: Path) -> tuple[bool, float | None, float | None]:
    """若新模型 AUC 更优则替换生产模型"""
    print("=" * 60)
    print("🧪 [3/4] 对比新旧模型")
    print("=" * 60)
    new_auc = load_auc(new_model)
    old_auc = load_auc(production)
    print(f"   新模型 AUC: {new_auc if new_auc is not None else 'N/A'}")
    print(f"   生产模型 AUC: {old_auc if old_auc is not None else 'N/A'}")

    if new_auc is None:
        print(
            "   ⚠️  [SKIP:auc_missing] 无法读取新模型 AUC（json 与 pkl 都没有），"
            "不替换生产模型 —— 这是可见的 SKIP，不是错误"
        )
        return False, new_auc, old_auc
    if new_auc < AUC_GUARD:
        print(f"   ⚠️  [SKIP:below_guard] 新模型 AUC {new_auc:.4f} 低于安全阈值 {AUC_GUARD}，不替换")
        return False, new_auc, old_auc
    if old_auc is not None:
        # min_improvement_margin：样本 ~300 时 AUC 差 0.01 在噪声内，
        # 没有边际就会把「统计上无法区分」的模型推上生产（design §2.1）。
        required = old_auc + MIN_IMPROVEMENT_MARGIN
        if new_auc + 1e-12 < required:
            print(
                f"   ⚠️  [SKIP:not_improved] 新模型 AUC {new_auc:.4f} "
                f"未超过当前 {old_auc:.4f} + 边际 {MIN_IMPROVEMENT_MARGIN:.4f}，保留当前模型"
            )
            return False, new_auc, old_auc

    # 备份当前生产模型
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if production.exists():
        backup_path = BACKUP_DIR / f"volatility_scorer_v3_{stamp}.pkl"
        shutil.copy2(production, backup_path)
        print(f"   [BACKUP] 已备份当前模型: {backup_path}")
        metrics_backup = BACKUP_DIR / f"volatility_scorer_v3_{stamp}.json"
        if production.with_suffix(".json").exists():
            shutil.copy2(production.with_suffix(".json"), metrics_backup)

    # 替换生产模型
    shutil.copy2(new_model, production)
    if new_model.with_suffix(".json").exists():
        shutil.copy2(new_model.with_suffix(".json"), production.with_suffix(".json"))
    print(f"   [OK] 已替换生产模型: {production}")
    return True, new_auc, old_auc


def write_report(report: dict) -> Path:
    """写入结构化报告"""
    report_dir = ROOT / "data/ml_training/reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"retrain_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"   [REPORT] 报告已保存: {report_path}")
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="月度自动重训练")
    parser.add_argument("--model-output", default=str(DEFAULT_MODEL), help="生产模型路径")
    parser.add_argument("--start-date", default=None, help="数据起始日期（默认全量）")
    args = parser.parse_args()

    started = datetime.now()
    report: dict = {
        "run_at": started.isoformat(),
        "status": "RUNNING",
        "steps": {},
    }

    try:
        # 1. 导出数据
        data_path = export_data(args.start_date)
        df = pd.read_csv(data_path)
        n_samples = len(df)
        report["samples"] = n_samples
        print(f"\n📊 训练样本数: {n_samples}")
        if n_samples < MIN_SAMPLES:
            print(f"   ⚠️  样本数 {n_samples} < {MIN_SAMPLES}，跳过训练")
            report["status"] = "SKIPPED_TOO_FEW_SAMPLES"
            write_report(report)
            return 0

        # 2. 训练新模型（临时文件）
        stamp = started.strftime("%Y%m%d_%H%M%S")
        new_model = ROOT / f"data/ml_models/volatility_scorer_candidate_{stamp}.pkl"
        train_model(data_path, new_model)

        # 3. 对比并替换
        production = Path(args.model_output)
        promoted, new_auc, old_auc = promote_if_better(new_model, production)
        report["new_auc"] = new_auc
        report["old_auc"] = old_auc
        report["promoted"] = promoted
        report["new_model"] = str(new_model)
        report["production_model"] = str(production)

        # 4. 清理临时文件
        if new_model.exists():
            new_model.unlink()
            print(f"   [CLEAN] 已清理临时模型: {new_model.name}")
        if new_model.with_suffix(".json").exists():
            new_model.with_suffix(".json").unlink()

        report["status"] = "SUCCESS" if promoted else "NO_CHANGE"
        report["duration_seconds"] = (datetime.now() - started).total_seconds()
        write_report(report)

        print("\n" + "=" * 60)
        print(f"[OK] 月度重训练完成: {'已更新生产模型' if promoted else '保持当前模型'}")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n[FAIL] 月度重训练失败: {e}")
        report["status"] = "FAILED"
        report["error"] = str(e)
        report["duration_seconds"] = (datetime.now() - started).total_seconds()
        write_report(report)
        return 1


if __name__ == "__main__":
    sys.exit(main())
