#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把风控参数从「全局 env」显式化到**账户级**配置（P0-7 迁移）。

**默认 dry-run**（只打印/落盘，不写库）；加 ``--apply`` 才写。

背景（``docs/risk_limits_tenant_scoping_design.md`` §6）：``RiskLimits`` 的 10 个资金/亏损
参数全部从 ``os.getenv`` 读，一个进程一套、两个租户共用。2026-10-22 夜模拟账户 ``20170229``
亏 $10,351.89 撞上**全局** $10,000 急停线被误停 —— 一个数字管两个资金量不同的账户。

本脚本只做**显式化**：让**账户级**配置成为第 ① 层，覆盖全局 env。
**不改数值** —— 逐账户数值由 Product 拍板，用 ``--set ACCOUNT:FIELD=VALUE`` 传入。

## 硬要求（Chief 划定，2026-10-23）

1. **必须读"生效值"**，不是裸 ``os.getenv``。直接跑脚本时 ``.env`` 并不在进程环境里
   （实测 ``RISK_MAX_DAILY_LOSS env = None``），裸读会拿到 ``RiskLimits`` 的 dataclass
   默认值 **20000/100000/5000**，而线上实际生效的是 **100000/100000/10000**。
   本脚本先 ``Settings.load(refresh=True)``（把 ``.env`` 折进 ``os.environ``）再取值。
2. **只写 REAL 且 ``is_active=True`` 的账户**。``real_account_config`` 是**实盘门禁**配置，
   给 SIMULATE 账户建这个条目语义就是错的；模拟账户走第二层 ``risk_limits:{tenant_id}``。
3. **备份 + 磁盘余量检查 + 走共享入口**；只写 ``real_account_config:{account_id}`` 的
   ``risk`` 段；**绝不修改 ``.env`` / ``RISK_*``**（env 保留为兜底默认）。
4. **写后回读比对，不一致就报错并回滚**（不是打印）。"写错数"只有回读能证伪。

## 键名口径（与 ``risk_limits.ACCOUNT_RISK_FIELDS`` 一致，不要在本文件另立一份）

``risk`` 段用 ``max_entry_notional`` / ``max_entry_daily_notional``（开仓侧），
账户**顶层** ``max_order_notional`` / ``max_daily_notional`` 是**实盘门禁**（real_trading.py），
**同名不同层、不同归属** —— 这是 Chief 10-23 的裁定，本脚本只读顶层做对照，**不改写**。

用法::

    python scripts/migrate_risk_limits_to_accounts.py                 # dry-run
    python scripts/migrate_risk_limits_to_accounts.py --apply         # 落库（先自动备份）
    python scripts/migrate_risk_limits_to_accounts.py --set 20170229:max_daily_loss=2000
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.tenant import (  # noqa: E402
    DEFAULT_TENANTS,
    REAL_ACCOUNT_CONFIG_KEY,
    TenantProfile,
    real_account_config_key,
    risk_limits_config_key,
)
from futu_quant.risk_limits import (  # noqa: E402
    ACCOUNT_RISK_FIELD_TO_LIMITS,
    ACCOUNT_RISK_FIELDS,
)

DEFAULT_DB = ROOT / "data" / "trading.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "qa"
DEFAULT_MIN_FREE_GB = 4.0

# 只做**前缀扫描**用（settings_with_prefix）；具体键一律走 real_account_config_key()。
ACCOUNT_CONFIG_PREFIX = f"{REAL_ACCOUNT_CONFIG_KEY}:"

RISK_FIELD_NAMES: tuple[str, ...] = tuple(name for name, _ in ACCOUNT_RISK_FIELDS)

# 账户**顶层**的实盘门禁额度（real_trading.py 用）—— 与 risk 段同名不同层，只读对照。
ACCOUNT_LEVEL_LIMITS = ("max_order_notional", "max_daily_notional")


def load_effective_risk_values() -> dict[str, float]:
    """当前**生效**的账户相关风险参数 —— 先让 ``.env`` 进环境，再读。

    为什么必须这样：``RiskLimits.load()`` 读 ``os.getenv``，而直接跑脚本时 ``.env``
    不在进程环境里（``_apply_dotenv`` 只在 ``Settings.load`` 里被调用）。裸读会拿到
    dataclass 默认值，与线上生效值不是一回事。
    """
    from futu_quant.config import Settings

    Settings.load(refresh=True)
    from futu_quant.risk import RiskLimits

    limits = RiskLimits.load()
    return {
        name: float(getattr(limits, ACCOUNT_RISK_FIELD_TO_LIMITS.get(name, name)))
        for name, _ in ACCOUNT_RISK_FIELDS
    }


@dataclass
class MigrationPlan:
    """一个账户的迁移计划。"""

    tenant_id: str
    account_id: str
    display_name: str
    action: str                       # "write" | "skip"
    reason: str = ""
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, float] = field(default_factory=dict)
    before_value: dict[str, float] = field(default_factory=dict)   # 迁移前生效值（全局）
    notes: list[str] = field(default_factory=list)
    layer: str = "account"                 # "account"=第①层 / "tenant"=第②层

    @property
    def key(self) -> str:
        """本计划要写的键。"""
        if self.layer == "tenant":
            return risk_limits_config_key(self.tenant_id)
        return real_account_config_key(self.account_id)

    @property
    def target(self) -> str:
        """给人看的写入目标描述。"""
        return f"{self.key} 的 risk 段" if self.layer == "account" else self.key

def _load_registry(path: str | None) -> dict[str, TenantProfile]:
    if not path:
        return dict(DEFAULT_TENANTS)
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    registry: dict[str, TenantProfile] = {}
    for tenant_id, item in raw.items():
        data = dict(item)
        data["tenant_id"] = tenant_id
        registry[tenant_id] = TenantProfile(**data)
    return registry


def _read_account_config(store, account_id: str) -> dict[str, Any]:
    raw = store.get_setting(real_account_config_key(account_id), "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_plan(
    store,
    registry: dict[str, TenantProfile],
    only_account: str | None = None,
    *,
    overrides: dict[str, dict[str, float]] | None = None,
    effective: dict[str, float] | None = None,
    seed_tenant_layer: bool = True,
) -> list[MigrationPlan]:
    """为每个启用的租户生成迁移计划（不写库）。

    **两层，按租户性质分**（三层解析链的前两层，env 是第三层兜底）：

    - **REAL 且启用** → 第 ① 层 ``real_account_config:{account_id}.risk``（账户级，最具体）
    - **非 REAL 且启用** → 第 ② 层 ``risk_limits:{tenant_id}``（租户级）

    **为什么非实盘必须写第 ② 层**（Chief 10-23 裁定）：``real_account_config`` 是**实盘门禁**
    配置，给 SIMULATE 账户建这个条目语义就是错的。而模拟账户**不会**有账户级条目 ——
    不写第 ② 层的话，它**永远只能 env 回落**，P0-7 只修了实盘那一半。
    种子值取**当前生效值**，因此**零行为变化**；此后 Product 可按账户资金量逐账户调。
    """
    overrides = overrides or {}
    effective = effective if effective is not None else load_effective_risk_values()
    plans: list[MigrationPlan] = []
    seen_accounts: set[str] = set()

    for tenant_id, profile in registry.items():
        account_id = str(profile.account_id)
        if only_account and account_id != str(only_account):
            continue
        seen_accounts.add(account_id)
        existing = _read_account_config(store, account_id)

        if not profile.is_active:
            plans.append(MigrationPlan(
                tenant_id=tenant_id, account_id=account_id, display_name=profile.display_name,
                action="skip", reason="租户未启用（is_active=False），不迁移",
                before=existing, after=existing,
            ))
            continue

        is_real = str(profile.trading_env).upper() == "REAL"

        # ---------- 第 ② 层：非实盘租户 → risk_limits:{tenant_id} ----------
        if not is_real:
            tenant_key = risk_limits_config_key(tenant_id)
            if not seed_tenant_layer:
                plans.append(MigrationPlan(
                    tenant_id=tenant_id, account_id=account_id, display_name=profile.display_name,
                    action="skip",
                    reason=f"未启用第 ② 层种子（--skip-tenant-seed）：{tenant_key}",
                    before=existing, after=existing,
                ))
                continue
            seeded = store.get_setting(tenant_key, "")
            if seeded:
                try:
                    parsed = json.loads(seeded)
                except (json.JSONDecodeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict) and parsed:
                    plans.append(MigrationPlan(
                        tenant_id=tenant_id, account_id=account_id, display_name=profile.display_name,
                        action="skip",
                        reason=f"{tenant_key} 已存在 —— 不覆盖既有显式配置，需 --overwrite-risk",
                        before=existing, after=existing, risk=dict(parsed),
                        before_value=dict(parsed), layer="tenant",
                    ))
                    continue
            plans.append(MigrationPlan(
                tenant_id=tenant_id, account_id=account_id, display_name=profile.display_name,
                action="write",
                reason=(
                    f"非实盘租户 → 写第 ② 层 {tenant_key}（种子值 = 当前生效值，**零行为变化**；"
                    "不写它则该租户永远只能 env 回落）"
                ),
                before=existing, after={},
                risk={name: effective[name] for name in RISK_FIELD_NAMES},
                before_value=dict(effective), layer="tenant",
            ))
            continue

        # ---------- 第 ① 层：实盘账户 → real_account_config:{account_id}.risk ----------
        already = existing.get("risk")
        if isinstance(already, dict) and already:
            plans.append(MigrationPlan(
                tenant_id=tenant_id, account_id=account_id, display_name=profile.display_name,
                action="skip",
                reason="账户已有 risk 段 —— 不覆盖既有显式配置（避免抹掉人工调整），需 --overwrite-risk",
                before=existing, after=existing, risk=dict(already),
                before_value=dict(already), layer="account",
            ))
            continue

        risk: dict[str, float] = {name: effective[name] for name in RISK_FIELD_NAMES}
        notes: list[str] = []
        for name in ACCOUNT_LEVEL_LIMITS:
            top = existing.get(name)
            if top is None:
                continue
            try:
                top_value = float(top)
            except (TypeError, ValueError):
                notes.append(f"账户顶层 {name}={top!r} 无法转为数值，跳过对照")
                continue
            risk_field = next(
                (r for r, lim in ACCOUNT_RISK_FIELD_TO_LIMITS.items() if lim == name), None
            )
            risk_value = risk.get(risk_field) if risk_field else None
            notes.append(
                f"同名不同层：账户顶层 `{name}`={top_value:g}（实盘门禁，real_trading.py，不改写）"
                + (f"；risk 段 `{risk_field}`={risk_value:g}（风控层）" if risk_field else "")
                + " —— 两者数值不同是设计，不是冲突"
            )
        for name, value in (overrides.get(account_id) or {}).items():
            if name not in RISK_FIELD_NAMES:
                raise SystemExit(
                    f"--set 的字段 {name!r} 不是 RiskLimits 的账户相关字段（{RISK_FIELD_NAMES}）"
                )
            old = risk.get(name)
            risk[name] = float(value)
            notes.append(f"{name}: {old!r} → {float(value):g}（人工指定）")

        merged = dict(existing)
        merged["risk"] = risk
        plans.append(MigrationPlan(
            tenant_id=tenant_id, account_id=account_id, display_name=profile.display_name,
            action="write", reason="账户缺少 risk 段 —— 写入**生效值**以显式化（账户级覆盖全局）",
            before=existing, after=merged, risk=risk,
            before_value=dict(effective), notes=notes, layer="account",
        ))

    for key in sorted(store.settings_with_prefix(ACCOUNT_CONFIG_PREFIX)):
        account_id = key[len(ACCOUNT_CONFIG_PREFIX):]
        if account_id in seen_accounts or (only_account and account_id != str(only_account)):
            continue
        plans.append(MigrationPlan(
            tenant_id="<未登记租户>", account_id=account_id, display_name="(未登记租户)",
            action="skip", reason="库里有账户配置但不在租户注册表中，需人工确认归属",
            before=_read_account_config(store, account_id),
            after=_read_account_config(store, account_id),
        ))
    return plans


def apply_plan(store, plan: MigrationPlan) -> bool:
    """把一份计划写回它自己那一层。

    - ``layer="account"`` → ``real_account_config:{account_id}`` 的 ``risk`` 段
    - ``layer="tenant"``  → ``risk_limits:{tenant_id}``（整值就是 risk 段）
    """
    if plan.action != "write":
        return False
    if plan.layer == "tenant":
        store.set_setting(
            risk_limits_config_key(plan.tenant_id),
            json.dumps(plan.risk, ensure_ascii=False, separators=(",", ":")),
        )
    else:
        store.set_setting(
            real_account_config_key(plan.account_id),
            json.dumps(plan.after, ensure_ascii=False, separators=(",", ":")),
        )
    store.audit("RISK_LIMITS_MIGRATED_TO_ACCOUNT", {
        "account_id": plan.account_id,
        "tenant_id": plan.tenant_id,
        "layer": plan.layer,
        "key": plan.key,
        "risk": plan.risk,
        "actor": "migrate_risk_limits_to_accounts",
    }, actor="cli:migrate_risk_limits")
    return True


def verify_written(store, plan: MigrationPlan) -> str | None:
    """**回读比对**：写完立刻回读新键，与计划值逐字段比对。

    返回 ``None`` 表示一致；否则返回不一致的说明。
    "写错数"只有回读能证伪 —— 打印出来给人看不算。
    """
    raw = store.get_setting(plan.key, "")
    if not raw:
        return f"回读为空：{plan.key} 不存在或写入丢失"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return f"回读不是合法 JSON：{exc}"
    if not isinstance(parsed, dict):
        return f"回读不是 JSON 对象：{type(parsed).__name__}"
    actual = parsed if plan.layer == "tenant" else parsed.get("risk")
    if not isinstance(actual, dict):
        return f"回读缺少 risk 段（{plan.key}）"
    for name in RISK_FIELD_NAMES:
        want = plan.risk.get(name)
        got = actual.get(name)
        if got is None:
            return f"回读缺字段 {name}"
        if float(got) != float(want):
            return f"回读不一致：{name} 计划 {want!r} ≠ 实际 {got!r}"
    return None


def restore_plan(store, plan: MigrationPlan, before_raw: str | None) -> None:
    """回滚：还原写入前的原值（原来没有就删键）。"""
    key = plan.key
    if before_raw is None:
        store.delete_setting(key)
    else:
        store.set_setting(key, before_raw)


def check_free_space(path: Path, min_free_gb: float) -> int:
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(str(path)).free
    if free < int(min_free_gb * 1024 ** 3):
        raise RuntimeError(
            f"磁盘余量不足：{path} 可用 {free / 1024 ** 3:.2f} GB，要求 ≥ {min_free_gb:g} GB。"
            "清理磁盘后重试 —— **绝不带着不足的空间做写迁移**。"
        )
    return free


def backup_database(db_path: Path, backup_dir: Path, min_free_gb: float) -> dict:
    """用 SQLite 在线备份 API 备份，并回读校验（1.6 GB 生产库）。"""
    check_free_space(backup_dir, min_free_gb)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"trading.db.bak-{stamp}.db"

    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            with dst:
                src.backup(dst)
            page_count = dst.execute("PRAGMA page_count").fetchone()[0]
            quick = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()

    digest = hashlib.sha256()
    with dest.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    verify = sqlite3.connect(str(dest))
    try:
        settings_rows = verify.execute("SELECT COUNT(*) FROM system_settings").fetchone()[0]
    finally:
        verify.close()

    manifest = {
        "source": str(db_path),
        "backup": str(dest),
        "size_bytes": dest.stat().st_size,
        "sha256": digest.hexdigest(),
        "page_count": page_count,
        "quick_check": quick,
        "system_settings_rows": settings_rows,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    dest.with_suffix(dest.suffix + ".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return manifest


def render_report(
    plans: Sequence[MigrationPlan], *, applied: bool, effective: dict[str, float],
    verify_failures: Sequence[str] | None = None,
    verified: bool | None = None,
) -> str:
    lines: list[str] = []
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    lines.append("# 风控参数显式化（→ real_account_config:{account}.risk）迁移报告")
    lines.append("")
    lines.append(f"- 生成时间：{stamp}")
    lines.append(f"- 模式：**{'APPLY（已写库）' if applied else 'DRY-RUN（未写库）'}**")
    lines.append("- 取值口径：**生效值**（`Settings.load(refresh=True)` 折入 `.env` 后读，非裸 `os.getenv`）")
    lines.append("- 迁移范围：REAL 且 is_active=True 的账户（模拟/未启用账户跳过）")
    lines.append("")
    lines.append("## 生效值（迁移后仍保留为兜底，**本脚本不改 .env**）")
    lines.append("")
    lines.append("| 字段 | 生效值 |")
    lines.append("| :--- | ---: |")
    for name in RISK_FIELD_NAMES:
        lines.append(f"| `{name}` | {effective[name]:g} |")
    lines.append("")

    writes = [p for p in plans if p.action == "write"]
    lines.append("## 逐账户计划")
    lines.append("")
    for plan in plans:
        flag = "✏️ 写入" if plan.action == "write" else "⏭️ 跳过"
        lines.append(f"### {flag} `{plan.key}` — {plan.display_name}（`{plan.tenant_id}`）")
        lines.append("")
        lines.append(f"- 原因：{plan.reason}")
        if plan.action == "write":
            lines.append("")
            lines.append("| 字段 | 迁移前（全局生效） | 写入 risk 段 | 从多少到多少 |")
            lines.append("| :--- | ---: | ---: | :--- |")
            for name in RISK_FIELD_NAMES:
                before = plan.before_value.get(name, effective.get(name))
                after = plan.risk.get(name)
                same = "值不变，归属由全局 → 账户" if before == after else "**变更**"
                lines.append(f"| `{name}` | {before:g} | {after:g} | {same} |")
            for n in plan.notes:
                lines.append("")
                lines.append(f"- {n}")
        lines.append("")

    lines.append("## 结论与风险提示")
    lines.append("")
    if writes and len({tuple(sorted(p.risk.items())) for p in writes}) == 1:
        lines.append(
            "⚠️ **所有待迁移账户的 risk 段取值完全相同**。这消除了「靠 env 隐式生效」，"
            "**但没有解决误杀根因** —— 2026-10-22 夜模拟账户 `20170229` 亏 $10,351.89 "
            "撞上全局 $10,000 急停线被误停。资金量不同的账户共用一条急停线，"
            "迁移后仍会一边太松、一边太紧。"
        )
        lines.append("")
        lines.append(
            "**必须由 Product 逐账户拍板数值**（尤其 `max_daily_loss`），"
            "用 `--set ACCOUNT:FIELD=VALUE` 传入后再 `--apply`。"
        )
    else:
        lines.append("✅ 各账户的 risk 段取值**已不同**，误杀根因已被逐账户数值覆盖。")
    lines.append("")
    lines.append(
        "> 本脚本只做**显式化**（把隐式 env 变成显式账户配置）：不改数值、不改 `.env`、"
        "不改账户顶层门禁额度、不碰任何订单/持仓/账本。"
    )
    lines.append("")

    if verify_failures:
        lines.append("## ⛔ 回读比对失败（已回滚）")
        lines.append("")
        for failure in verify_failures:
            lines.append(f"- {failure}")
        lines.append("")
    elif applied and verified:
        lines.append("## ✅ 回读比对通过")
        lines.append("")
        lines.append(
            "`--apply` 写入后**立即回读**新键，与计划值**逐字段比对**，全部一致。"
            "（不一致会报错并回滚，不是只打印。）"
        )
        lines.append("")
    lines.append("")
    return "\n".join(lines)


def _parse_overrides(raw: Sequence[str]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for item in raw:
        left, _, value = item.partition("=")
        account, _, field_name = left.partition(":")
        account, field_name, value = account.strip(), field_name.strip(), value.strip()
        if not account or not field_name or not value:
            raise SystemExit(f"--set 需要 ACCOUNT:FIELD=VALUE 形式，收到 {item!r}")
        try:
            out.setdefault(account, {})[field_name] = float(value)
        except ValueError as exc:
            raise SystemExit(f"--set 的值必须是数值：{item!r}") from exc
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把风控参数显式化到账户级配置（P0-7）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument("--account", default=None, help="只处理指定账户 ID")
    parser.add_argument("--registry", default=None, help="自定义租户注册表 JSON 路径")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="TradingStore 数据库路径")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB)
    parser.add_argument("--skip-backup", action="store_true", help="跳过备份（仅在已确认有可用备份时）")
    parser.add_argument("--overwrite-risk", action="store_true", help="允许覆盖已存在的 risk 段")
    parser.add_argument(
        "--skip-tenant-seed", action="store_true",
        help="不给非实盘租户写第 ② 层 risk_limits:{tenant_id} 种子（默认会写）",
    )
    parser.add_argument(
        "--set", action="append", default=[], metavar="ACCOUNT:FIELD=VALUE",
        help="人工指定某账户的某字段值，可重复（如 20170229:max_daily_loss=2000）",
    )
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    from futu_quant.store import TradingStore

    overrides = _parse_overrides(args.set)
    effective = load_effective_risk_values()
    registry = _load_registry(args.registry)
    store = TradingStore(args.db)
    plans = build_plan(
        store, registry, only_account=args.account, overrides=overrides, effective=effective,
        seed_tenant_layer=not args.skip_tenant_seed,
    )

    print("=== 风控参数租户化迁移 · "
          f"{'APPLY（落库）' if args.apply else 'DRY-RUN（只读，不改库）'} ===")
    print()
    print("生效值口径：Settings.load(refresh=True) 折入 .env 后读取（非裸 os.getenv）")
    print(f"生效值：{json.dumps(effective, ensure_ascii=False)}")
    print("迁移范围（两层，按租户性质分）：")
    print("  · REAL 且启用的账户 → 第 ① 层 real_account_config:{account_id} 的 risk 段")
    print("  · 非 REAL 且启用的租户 → 第 ② 层 risk_limits:{tenant_id}（种子值 = 生效值，零行为变化）")
    print("  · 未启用租户 → 跳过")
    print()

    if not plans:
        print("没有匹配的账户。")
        return 0

    for plan in plans:
        flag = {"write": "✏️  写入", "skip": "⏭️  跳过"}[plan.action]
        layer = "①账户层" if plan.layer == "account" else "②租户层"
        print(f"{flag}  {plan.display_name}  账户 {plan.account_id}  ({plan.tenant_id})  [{layer}]")
        print(f"      键：{plan.key}")
        print(f"      原因：{plan.reason}")
        if plan.action == "write":
            print("      参数集（迁移前全局生效 → 写入值）：")
            for name in RISK_FIELD_NAMES:
                before = plan.before_value.get(name, effective.get(name))
                print(f"        {name:<34} {before:g} → {plan.risk.get(name):g}")
            for n in plan.notes:
                print(f"      · {n}")
        print()

    writes = [p for p in plans if p.action == "write"]
    print(f"合计：{len(plans)} 个对象，{len(writes)} 个将写入，{len(plans) - len(writes)} 个跳过。")
    print("env 的 RISK_* 值**原样保留**，继续作为兜底默认（本脚本不改 .env）。")
    if writes:
        print("⚠️  迁移只做显式化、**不改变数值**：请按账户资金量分别调整，")
        print("    尤其是模拟账户的 max_daily_loss（10-22 误杀的根因）——")
        print("    第 ② 层种子只是把 env 的值显式化，**逐账户调参是下一步**。")
    print()

    report_path = args.report or (
        args.backup_dir
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_risk_limits_to_accounts_"
          f"{'apply' if args.apply else 'dryrun'}.md"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(plans, applied=False, effective=effective), encoding="utf-8",
    )
    print(f"报告已落盘：{report_path}")

    if not args.apply:
        print()
        print("DRY-RUN 结束，未写库。复核报告后：停 Worker → --apply → 立刻 start。")
        return 0

    # ---------------- APPLY ----------------
    print()
    if not args.skip_backup:
        manifest = backup_database(args.db, args.backup_dir, args.min_free_gb)
        print(f"备份完成：{manifest['backup']}")
        print(f"  大小 {manifest['size_bytes'] / 1024 ** 3:.2f} GB，"
              f"page_count={manifest['page_count']}，quick_check={manifest['quick_check']}")
        print(f"  SHA256 {manifest['sha256']}")
    else:
        print("⚠️ 已跳过备份（--skip-backup）—— 请确认已有可用备份")

    # 写入前抓原值，供回读失败时回滚（键随层不同）
    before_raw = {
        plan.key: store.get_setting(plan.key, None)
        for plan in plans if plan.action == "write"
    }

    written = 0
    for plan in plans:
        if plan.action != "write":
            continue
        if plan.layer == "account" and not args.overwrite_risk and plan.before.get("risk"):
            print(f"跳过 {plan.key}（已有 risk 段，需 --overwrite-risk）")
            continue
        apply_plan(store, plan)
        written += 1
    print(f"已写入 {written} 个对象（账户层 / 租户层）。")

    # 回读比对 —— 不一致就报错并回滚
    failures = []
    for plan in plans:
        if plan.action != "write":
            continue
        failure = verify_written(store, plan)
        if failure:
            failures.append(f"{plan.key}: {failure}")
    if failures:
        print()
        print("[ERROR] 回读比对失败 —— 立即回滚本次全部写入：")
        for failure in failures:
            print(f"  - {failure}")
        for plan in plans:
            if plan.action != "write":
                continue
            restore_plan(store, plan, before_raw.get(plan.key))
        print("已回滚到写入前的状态（原值缺失的键已删除）。")
        report_path.write_text(
            render_report(plans, applied=False, effective=effective, verify_failures=failures),
            encoding="utf-8",
        )
        print(f"报告已落盘（含失败明细）：{report_path}")
        return 4

    print("回读比对通过：每个字段与计划值逐字段一致。")
    after = load_effective_risk_values()
    if after != effective:
        print("[ERROR] 生效值在迁移过程中发生了变化 —— 立即检查！")
        return 5
    print("不变量通过：RISK_* 生效值逐项不变（仍作兜底）")
    print()
    report_path.write_text(
        render_report(plans, applied=True, effective=effective, verified=True),
        encoding="utf-8",
    )
    print(f"APPLY 报告已覆盖落盘：{report_path}")
    print()
    print("⚠️ 立刻用新代码 start（中间不得留旧 Worker）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
