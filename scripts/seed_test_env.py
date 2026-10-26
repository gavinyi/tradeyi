#!/usr/bin/env python3
"""P0-5 验收用的最小数据集 seed（设计 §8.1）。

**为什么需要**：worktree 的 `data/` 是 gitignored 的、空的 —— pytest 用 tmp DB 无影响，
但**手动冒烟测试没有 DB / 配置，跑不起来**。不 seed 的话，「验收通过」可能只是
「没跑起来」——**又一种合格证**。

🔴 **本脚本自带「不许写生产」守卫**（设计 §8.1-4）：
`TradingStore()` 的默认路径是 **CWD 相对的 `data/trading.db`** ——
**从生产目录跑一次 seed，写的就是生产库**（与 10-23 那次事故同一形态的高频操作）。
所以这里在写任何东西之前，先断言 **CWD 不是生产根** 且 **DB 路径落在本 worktree 内**。

用法（**必须在 worktree 根目录下执行**）::

    python scripts/seed_test_env.py            # 写入 worktree 内的 data/trading.db
    python scripts/seed_test_env.py --dry-run  # 只校验守卫与内容，不写
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_ROOT = Path(r"E:\Thisyi\trade")

# seed 内容（**全部是测试值，不含任何生产数据**）
SEED_TENANT = "futu_real_master"
SEED_CANDIDATE_QUALITY = {
    # 实盘：软线收紧到 5%，硬线保持现值 12%，入场上限 = **真实门禁 12%**
    # （与 hard 相同，避免误导 —— 上界必须等于真实入场门禁，不能写成 20）
    "soft_spread_pct": 5.0,
    "hard_spread_pct": 12.0,
    "entry_max_spread_pct": 12.0,
    "hard_oi_min": 500,
    "hard_moneyness_max": 10.0,
    "hard_stale_seconds": 180,
    "entry_max_stale_seconds": 300,
}
SEED_CAMPAIGN = {
    "campaign_id": "SEED-META-1",
    "underlying": "US.META",
    "code": "US.META261030P660000",
    "trigger_mode": "VOLATILITY",
    "tenant_id": SEED_TENANT,
    "maturity_min_dte": 5,
    "maturity_max_dte": 21,
    "max_premium_per_stage": 1000,
}


def assert_not_production(db_path: Path) -> None:
    """**写之前必须过这一关。** 不过就直接退出，绝不"先写了再说"。"""
    cwd = Path.cwd().resolve()
    if cwd == PRODUCTION_ROOT:
        raise SystemExit(
            f"拒绝执行：当前目录就是生产根 {PRODUCTION_ROOT} —— "
            "在这里跑 seed 会直接写生产库。请切到 worktree 根目录再跑。"
        )
    resolved_db = db_path.resolve()
    if ROOT not in resolved_db.parents:
        raise SystemExit(
            f"拒绝执行：DB 路径 {resolved_db} 不在本 worktree {ROOT} 内 —— "
            "可能写穿到别的库。"
        )
    if PRODUCTION_ROOT in resolved_db.parents:
        raise SystemExit(f"拒绝执行：DB 路径落在生产目录内：{resolved_db}")


def main() -> int:
    parser = argparse.ArgumentParser(description="P0-5 验收最小数据集 seed")
    parser.add_argument("--dry-run", action="store_true", help="只校验守卫与内容，不写库")
    args = parser.parse_args()

    db_path = ROOT / "data" / "trading.db"
    assert_not_production(db_path)

    print(f"worktree   : {ROOT}")
    print(f"cwd        : {Path.cwd().resolve()}")
    print(f"目标 DB    : {db_path}")
    print(f"seed 租户  : {SEED_TENANT}")
    print(f"candidate_quality: {json.dumps(SEED_CANDIDATE_QUALITY, ensure_ascii=False)}")
    print(f"seed campaign    : {SEED_CAMPAIGN['campaign_id']} / {SEED_CAMPAIGN['code']}")

    if args.dry_run:
        print("--dry-run：守卫通过，未写任何数据。")
        return 0

    sys.path.insert(0, str(ROOT / "src"))
    from futu_quant.candidate_quality import (  # noqa: PLC0415
        candidate_quality_key, resolve_candidate_quality,
    )
    from futu_quant.store import TradingStore  # noqa: PLC0415

    # A1（2026-10-27）：测试区播种脚本允许显式建库（env 不再授权 bootstrap）
    store = TradingStore(db_path, bootstrap=True)
    store.set_setting(
        candidate_quality_key(SEED_TENANT),
        json.dumps(SEED_CANDIDATE_QUALITY, ensure_ascii=False),
    )
    config = resolve_candidate_quality(store, SEED_TENANT)
    print(f"已写入并回读校验：{config.as_dict()}")
    print("seed 完成。**冒烟测试前必须跑这一步**，否则「验收通过」可能只是「没跑起来」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
