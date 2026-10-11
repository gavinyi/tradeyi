"""
手动/快捷同步当前生效标的池至运行时
"""
from __future__ import annotations

from futu_quant.store import TradingStore
from futu_quant.universe.lifecycle_manager import LifecycleManager


def main() -> None:
    store = TradingStore()
    manager = LifecycleManager(store=store)
    merged = manager.sync_to_runtime_universe()
    promoted = store.active_promoted_symbols()

    print(f"=== 标的池同步成功 (共 {len(merged)} 只) ===")
    print("生效列表:", merged)
    print(f"活跃外援 ({len(promoted)} 只):", [r["symbol"] for r in promoted])


if __name__ == "__main__":
    main()
