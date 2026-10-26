"""ML 重训后台入口（P2 区块②）。

为什么必须有这个 wrapper，而不是让页面/服务直接调用 `run_retrain`：

- **页面**：训练可能跑几分钟到几十分钟，Streamlit 里同步跑会把界面卡死；
- **服务主循环**：`run_service` 一轮里有交易任务，阻塞 30 分钟等于掉线。

所以页面只写一个请求标记（或直接 Popen 本脚本），真正的训练在这里跑：
- 文件锁保证并发触发只有一个在跑（第二个拿到 `SKIP:already_running`）；
- 每次触发都在 `ml_retrain_runs` 留一行（SKIP 也留）；
- 缺 `sklearn` → `SKIP:sklearn_missing`，**不当作错误**（依赖安装由用户决定节奏）。

用法：
    python scripts/run_ml_retrain_wrapper.py --triggered-by panel --kind manual
    python scripts/run_ml_retrain_wrapper.py --triggered-by scheduler --kind auto --no-promote
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from futu_quant.ml_retrain import run_retrain  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="ML 重训后台入口")
    parser.add_argument("--triggered-by", default="panel")
    parser.add_argument("--kind", default="manual", choices=("manual", "auto"))
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument(
        "--no-promote", action="store_true",
        help="只训练并落候选模型，绝不覆盖生产模型（自动重训必须带）",
    )
    args = parser.parse_args()

    store = TradingStore()
    result = run_retrain(
        store, triggered_by=args.triggered_by, trigger_kind=args.kind,
        timeout_seconds=args.timeout, no_promote=args.no_promote,
    )
    print(f"[ML-RETRAIN] status={result.get('status')} reason={result.get('reason', '')}")
    # SKIP 也是正常结论（缺依赖/已在跑），返回 0 让调用方不要把它当失败
    return 0 if result.get("status") in {"SUCCESS", "SKIP"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
