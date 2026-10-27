"""租户启用开关（`tenant_active:<tenant_id>`）的读写入口。

用法::

    python scripts/set_tenant_active.py --list
    python scripts/set_tenant_active.py --tenant futu_real_master --off
    python scripts/set_tenant_active.py --tenant futu_real_master --on

为什么要这个脚本：2026-10-28 之前租户启用状态是 `src/futu_quant/tenant.py` 里的
**代码常量**（`TenantProfile.is_active=True`），界面上「关掉实盘账号」并不会改变它，
`start.bat` 照旧拉起两个 Worker；而实盘交易关闭（`REAL_TRADING_ENABLED=false`）后
实盘 Worker 的预检必然失败，被误判成「OpenD 掉线」并推送 CRITICAL。

关掉一个租户后：`launch_workers.py` 不再为它起 Worker；即使有人手工拉起，
Worker 也会在启动时读到 `tenant_active:<id>=false` 并立刻退出（不空转、不刷告警）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.store import TradingStore  # noqa: E402
from futu_quant.tenant import (  # noqa: E402
    DEFAULT_TENANTS,
    resolve_tenant_active,
    tenant_active_key,
)


def _format(value: bool) -> str:
    return "true" if value else "false"


def main() -> int:
    parser = argparse.ArgumentParser(description="租户启用开关（配置驱动，替代代码常量）")
    parser.add_argument("--tenant", help="租户 ID，例如 futu_real_master")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true", help="启用该租户")
    group.add_argument("--off", action="store_true", help="停用该租户")
    parser.add_argument("--list", action="store_true", help="列出所有租户当前启用状态")
    parser.add_argument("--actor", default="ops:set_tenant_active", help="审计用的操作者标识")
    args = parser.parse_args()

    store = TradingStore()
    if args.list or not args.tenant:
        for tenant_id, profile in DEFAULT_TENANTS.items():
            enabled = resolve_tenant_active(tenant_id, store=store)
            raw = store.get_setting(tenant_active_key(tenant_id), "")
            source = "配置键" if str(raw).strip() != "" else "注册表默认"
            print(
                f"{tenant_id:24s} {profile.display_name:22s} "
                f"{'启用' if enabled else '停用'}  ({source})"
            )
        return 0

    tenant_id = str(args.tenant).strip()
    if tenant_id not in DEFAULT_TENANTS:
        print(f"[x] 未知租户: {tenant_id}；可选: {', '.join(DEFAULT_TENANTS)}")
        return 2
    if not (args.on or args.off):
        print("[x] 需要 --on 或 --off（或用 --list 查看）")
        return 2

    value = bool(args.on)
    store.set_setting(tenant_active_key(tenant_id), _format(value))
    store.operational_event(
        "tenant_registry",
        "TENANT_ACTIVE_CHANGED",
        "INFO",
        {"tenant_id": tenant_id, "active": value, "actor": args.actor},
    )
    # 读回校验：报告以**读回值**为准，不凭写入成功下结论
    readback = resolve_tenant_active(tenant_id, store=store)
    print(
        f"[ok] {tenant_id} -> {'启用' if value else '停用'}；"
        f"读回={_format(readback)}（键 {tenant_active_key(tenant_id)}）"
    )
    if readback != value:
        print("[x] 读回与写入不一致，请检查 system_settings")
        return 3
    print("提示：重启服务（stop.bat → start.bat）后生效；launch_workers 会按开关决定起几个 Worker。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
