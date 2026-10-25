#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把无前缀全局键 ``VOLATILITY_CAMPAIGNS`` 的存量 campaign 按租户分区。

**默认 dry-run**：只打印/落盘报告，不写库。加 ``--apply`` 才写。

归属判据（**按证据，不猜**，优先级由高到低）：

1. campaign 内已有 ``tenant_id`` / ``account_id`` 字段 → 用它；
2. 该 ``code`` 在某账户**券商持仓**里 → 归该账户的租户（来源 ``broker_snapshots``）；
3. 该 ``code`` 的**成交意图**（``order_intents.tenant_id``）→ 用它；
   若多租户都有意图，取**最近一条意图**的租户（平局再比意图条数、再比 tenant_id），
   并标 ``AMBIGUOUS`` —— 宁可显式标注，也不假装没有歧义；
4. 都不满足 → 归**雷达扫描所用账户**的租户，**并记 WARN 审计，不静默**。

红线（与 ``docs/volatility_campaigns_tenant_partition_design.md`` 一致）：

- **只写** ``VOLATILITY_CAMPAIGNS:{tenant_id}``；
- 无前缀旧键 **原样保留、逐字节不变**（只读 inert，永不删除）；
- **不碰任何订单、持仓、账本** —— 真金零风险；
- ``--apply`` 前**必须**先备份 ``data/trading.db``（磁盘余量 < 4 GB 直接拒绝）。

用法::

    # 1) 先看 dry-run（报告落 data/qa/）
    $env:PYTHONPATH="src"; .\\.venv\\Scripts\\python.exe scripts\\migrate_volatility_campaigns_tenant_scope.py

    # 2) 人工复核报告后，停 Worker → apply → 立刻 start（背靠背，中间不留旧 Worker）
    ... migrate_volatility_campaigns_tenant_scope.py --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.tenant import (  # noqa: E402
    VOLATILITY_CAMPAIGNS_KEY,
    campaigns_config_key,
    legacy_config_key,
)


def _print_safe(*args, **kwargs) -> None:
    """打印时容忍非 UTF-8 控制台（Windows GBK 双击/命令行会踩）。

    emoji 在 GBK 流上抛 `UnicodeEncodeError`：手工跑脚本会直接崩，`pytest -s` 也会假红。
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        fallback = " ".join(str(arg) for arg in args)
        print(fallback.encode("ascii", "replace").decode("ascii"), **kwargs)

DEFAULT_DB = ROOT / "data" / "trading.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "qa"
DEFAULT_MIN_FREE_GB = 4.0
DB_SIZE_BYTES = 1.6 * 1024 ** 3  # 已实测约 1.6 GB（2026-10-23）

# 设计文档 docs/volatility_campaigns_tenant_partition_design.md 的口径：11 条。
# 2026-10-23 实测无前缀旧键只有 8 条 —— 差异必须显式留痕，
# **不得默默按实测值抹掉设计口径**。差异原因待 Architect 核对（Chief 已安排）。
DESIGN_DOC_EXPECTED_CAMPAIGNS = 11

RULE_FIELD = "1-campaign-field"
RULE_POSITION = "2a-net-position"
RULE_POSITION_SNAPSHOT = "2b-broker-position-snapshot"
RULE_INTENT = "3-order-intent"
RULE_EXIT_ONLY = "4a-exit-only-inferred"
RULE_FALLBACK = "4b-no-evidence-pending"
RULE_OVERRIDE = "0-manual-override"

#: 待人工确认 —— **不猜**（Chief 10-23 裁定）
STATUS_PENDING = "pending"

# 主连接（plain ``positions`` / ``account_info``）快照 → 租户的兜底判据
PRIMARY_SUFFIX = "positions"

# 账户号 → 快照后缀（run_service.py:704/725 写死的名字，不含账号本身）
ACCOUNT_SUFFIXES = {
    "281756481476169109": "futuhk_real",
    "283445327916889204": "moomoo_real",
}


@dataclass
class Attribution:
    """一条 campaign 的归属判定。"""

    code: str
    tenant_id: str
    rule: str
    evidence: str
    ambiguous: bool = False
    warnings: tuple[str, ...] = ()
    detail: dict = field(default_factory=dict)
    status: str = "assigned"     # "assigned" | "pending"

    @property
    def rule_label(self) -> str:
        return {
            RULE_OVERRIDE: "0 人工指定",
            RULE_FIELD: "① campaign 自带 tenant_id/account_id",
            RULE_POSITION: "② 净持仓归属（窗口快照，**权威**）",
            RULE_POSITION_SNAPSHOT: "②b 仅 broker_snapshots 佐证（成交流水无记录）",
            RULE_INTENT: "③ 成交意图带 tenant_id（当前净持仓为 0）",
            RULE_EXIT_ONLY: "④a 无持仓，按 exit_only + 接管审计推定",
            RULE_FALLBACK: "④b 无持仓且推定不出 → 默认归模拟租户（无则待人工确认）",
        }[self.rule]

    @property
    def pending(self) -> bool:
        return self.status == STATUS_PENDING

    @property
    def warn(self) -> bool:
        return bool(self.warnings) or self.pending or self.rule in {
            RULE_FALLBACK, RULE_EXIT_ONLY,
        }


# --------------------------------------------------------------------------
# 证据收集
# --------------------------------------------------------------------------


def _load_json_list(raw: str) -> list[dict]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _load_env_defaults() -> tuple[str, str]:
    """雷达自己用的那套 env 默认值（``run_radar_service.py:107`` 即 ``Settings.load(refresh=True)``）。

    必须与雷达一致，否则判据④的兜底租户会与真正写 campaign 的那个进程不是同一个。
    """
    try:
        from futu_quant.config import Settings

        settings = Settings.load(refresh=True)
        env = str(getattr(settings, "trading_env", "") or "").strip().upper()
        account = str(getattr(settings, "real_account_id", "") or "").strip()
    except Exception:  # noqa: BLE001
        env, account = "", ""
    return (
        env or (os.getenv("FUTU_TRADING_ENV") or "REAL").strip().upper(),
        account or (os.getenv("REAL_ACCOUNT_ID") or "").strip(),
    )


def resolve_primary_tenant(
    tenants: Sequence,
    *,
    trading_env: str | None = None,
    account_id: str | None = None,
):
    """主连接（雷达 / 单体模式）所用的租户。

    雷达 ``run_radar_service.py:107`` 用 ``FutuPaperBroker(Settings.load(refresh=True))``，
    不带租户覆盖 —— 即 ``FUTU_TRADING_ENV`` + ``REAL_ACCOUNT_ID`` 决定的那一个。
    """
    from futu_quant.tenant import resolve_tenant

    env = (trading_env or "").strip().upper()
    acc = (account_id or "").strip()
    if not env:
        env, acc = _load_env_defaults()
    try:
        return resolve_tenant(account_id=acc, env=env) or resolve_tenant(env=env)
    except Exception:  # noqa: BLE001
        return resolve_tenant(env=env)


def positions_snapshot_index(
    con: sqlite3.Connection,
    tenants: Sequence,
    *,
    primary_tenant,
) -> tuple[dict[str, list[dict]], list[str]]:
    """把 ``broker_snapshots`` 里的持仓快照映射到租户。

    匹配按**确定性优先级**，不做模糊猜测：

    1. 账户专属后缀（``{account_id}`` / 账号别名如 ``futuhk_real``）→ 该账户的租户；
    2. 环境后缀（``positions_real`` / ``positions_sim`` / ``..._primary``）→
       **主连接租户**（雷达用的那一个）；主连接 env 不匹配时退到该 env 的唯一租户；
    3. ``positions`` → 主连接租户。

    匹配不上的快照键进 ``unmapped`` 并在报告里显式列出 —— **宁可少一条证据，
    也不把无关快照算成某账户的持仓**。
    """
    index: dict[str, list[dict]] = {t.tenant_id: [] for t in tenants}
    unmapped: list[str] = []
    try:
        rows = list(con.execute(
            "SELECT snapshot_key, payload_json, captured_at FROM broker_snapshots "
            "WHERE snapshot_key LIKE 'positions%' ORDER BY captured_at DESC"
        ))
    except sqlite3.OperationalError:
        # 库结构不含快照表（旧库 / 隔离测试库）→ 无判据②证据，不猜
        return index, unmapped
    if not rows:
        return index, unmapped

    account_suffixes: dict[str, set[str]] = {}
    for t in tenants:
        account_suffixes[t.tenant_id] = {
            t.account_id, ACCOUNT_SUFFIXES.get(t.account_id, ""),
        } - {""}

    primary_tid = primary_tenant.tenant_id if primary_tenant else None
    env_suffixes = {
        t.tenant_id: {
            t.trading_env.lower(),
            f"{t.trading_env.lower()}_primary",
            "real" if t.trading_env == "REAL" else "sim",
        }
        for t in tenants
    }

    for row in rows:
        key, payload, captured_at = row[0], row[1], row[2]
        parts = key.split("_", 1)
        suffix = parts[1] if len(parts) == 2 else ""
        codes = {
            str(item.get("code") or item.get("stock_code") or "").strip()
            for item in _load_json_list(payload)
        }
        codes.discard("")

        matched: str | None = None
        for tid, sfx in account_suffixes.items():
            if suffix in sfx:
                matched = tid
                break
        if matched is None and suffix:
            # 环境后缀：优先主连接租户；否则取该 env 下唯一租户
            if primary_tid and suffix in env_suffixes[primary_tid]:
                matched = primary_tid
            else:
                env_match = [
                    tid for tid, sfx in env_suffixes.items()
                    if suffix in sfx and (not primary_tid or tid != primary_tid)
                ]
                if len(env_match) == 1:
                    matched = env_match[0]
        if matched is None and key == PRIMARY_SUFFIX and primary_tid:
            matched = primary_tid
        if matched is None:
            unmapped.append(key)
            continue
        index[matched].append({"key": key, "captured_at": captured_at, "codes": codes})
    return index, unmapped


def position_codes_for(index: dict[str, list[dict]], tenant_id: str) -> dict[str, str]:
    """``{code: 来源快照键}``（同一 code 多个快照时取最新一条）。"""
    out: dict[str, str] = {}
    for snap in sorted(index.get(tenant_id, []), key=lambda s: s["captured_at"]):
        for code in snap["codes"]:
            out[code] = snap["key"]
    return out


def intent_tenants(con: sqlite3.Connection, code: str) -> list[dict]:
    """该 code 的成交意图按租户聚合，按最近意图时间倒序。"""
    return [
        {"tenant_id": row[0], "count": row[1], "latest": row[2]}
        for row in con.execute(
            "SELECT COALESCE(tenant_id,'') AS tenant_id, COUNT(*) AS count, "
            "MAX(created_at) AS latest FROM order_intents WHERE symbol=? "
            "GROUP BY tenant_id ORDER BY latest DESC, count DESC, tenant_id ASC",
            (code,),
        )
    ]


def net_position_by_tenant(con: sqlite3.Connection, code: str) -> list[dict]:
    """按 ``order_intents`` 的 **FILLED** 记录推算该 code 的**净持仓**（按租户）。

    **为什么不能只靠 ``broker_snapshots``**（10-23 Chief 实测）：
    快照是**某一时刻的缓存**，会过期/不全 —— 模拟账户的
    ``US.QQQ261029C735000 +1`` 在快照里根本不存在，只按快照判会把有真实持仓的
    campaign 判给另一租户，**迁移那一刻就让它失去管理**（10-22 META 的失效模式）。

    成交意图是**流水**：BUY 加、SELL 减，按租户聚合后非零即"该租户当前持有"。
    """
    try:
        rows = list(con.execute(
            "SELECT COALESCE(tenant_id,'') AS tenant_id, "
            "SUM(CASE WHEN UPPER(side)='BUY' THEN COALESCE(filled_quantity,0) "
            "         WHEN UPPER(side)='SELL' THEN -COALESCE(filled_quantity,0) ELSE 0 END) AS net "
            "FROM order_intents WHERE symbol=? AND UPPER(COALESCE(status,''))='FILLED' "
            "GROUP BY tenant_id ORDER BY net DESC",
            (code,),
        ))
    except sqlite3.OperationalError:
        return []
    return [
        {"tenant_id": r[0], "net": float(r[1] or 0)}
        for r in rows if abs(float(r[1] or 0)) > 0
    ]


def load_positions_file(
    path: Path,
    *,
    tenants: Sequence,
    max_age_minutes: float = 30.0,
) -> dict[str, set[str]]:
    """读窗口内产出的**持仓快照**，返回 ``{tenant_id: {code, ...}}``（只含 qty != 0）。

    **为什么必须显式吃这份快照**（Coder 10-23 提出，Chief 采纳）：
    迁移脚本**没有 broker**，拿不到两个账户的持仓。若让它自己假设"都没有持仓"，
    就会把有真实持仓的 campaign 判给另一租户 —— **迁移那一刻就失效**。

    **fail-closed**：以下任一情况直接抛异常，**绝不降级成"假设无持仓"**：

    - 文件不存在 / 不是合法 JSON / 结构不符；
    - 缺 ``captured_at``，或快照**超过 ``max_age_minutes`` 分钟**（过期即失效）；
    - 某个**启用**租户没有出现，或出现多次；
    - 持仓行缺 ``code``，或 ``qty`` 不是数值。
    """
    if not path.exists():
        raise RuntimeError(f"持仓快照不存在：{path} —— 拒绝在无证的情况下 apply")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"持仓快照不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("持仓快照顶层必须是 JSON 对象")

    captured_at = str(raw.get("captured_at") or "").strip()
    if not captured_at:
        raise RuntimeError("持仓快照缺 captured_at —— 无法判断是否过期，拒绝 apply")
    try:
        captured = datetime.fromisoformat(captured_at)
    except ValueError as exc:
        raise RuntimeError(f"captured_at 不是合法时间：{captured_at!r}") from exc
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=datetime.now().astimezone().tzinfo)
    age_minutes = (datetime.now().astimezone() - captured).total_seconds() / 60.0
    if age_minutes > max_age_minutes:
        raise RuntimeError(
            f"持仓快照已过期：captured_at={captured_at}（{age_minutes:.1f} 分钟前，"
            f"上限 {max_age_minutes:g} 分钟）—— 拒绝 apply，请重新产出快照"
        )

    accounts = raw.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError("持仓快照缺 accounts 数组或为空")

    active_ids = {t.tenant_id for t in tenants if getattr(t, "is_active", False)}
    seen: set[str] = set()
    result: dict[str, set[str]] = {}
    for i, item in enumerate(accounts):
        if not isinstance(item, dict):
            raise RuntimeError(f"accounts[{i}] 不是对象")
        tid = str(item.get("tenant_id") or "").strip()
        if not tid:
            raise RuntimeError(f"accounts[{i}] 缺 tenant_id")
        if tid not in active_ids:
            # 未启用租户不参与分配，忽略但不报错
            continue
        if tid in seen:
            raise RuntimeError(f"accounts[] 里 {tid} 出现多次 —— 拒绝 apply")
        seen.add(tid)
        positions = item.get("positions")
        if not isinstance(positions, list):
            raise RuntimeError(f"{tid} 的 positions 不是数组")
        codes: set[str] = set()
        for j, pos in enumerate(positions):
            if not isinstance(pos, dict):
                raise RuntimeError(f"{tid}.positions[{j}] 不是对象")
            code = str(pos.get("code") or "").strip()
            if not code:
                raise RuntimeError(f"{tid}.positions[{j}] 缺 code")
            try:
                qty = float(pos.get("qty"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"{tid}.positions[{j}] 的 qty 不是数值：{pos.get('qty')!r}"
                ) from exc
            if qty != 0:
                codes.add(code)
        result[tid] = codes

    missing = active_ids - seen
    if missing:
        raise RuntimeError(
            f"持仓快照缺启用租户 {sorted(missing)} —— 拒绝 apply（缺失不等于无持仓）"
        )
    return result


def _orphan_adopt_source(con: sqlite3.Connection, code: str) -> str:
    """接管审计里该 code 的来源租户（``VOLATILITY_ORPHAN_ADOPTED`` / ``*_ADOPTED*``）。

    找不到返回 ``""``。用于"两账户都不持有"时的**推定**依据之一 ——
    有接管记录说明该 code 曾被某租户接管过，归属倾向它。
    """
    try:
        rows = list(con.execute(
            "SELECT strategy_id, payload_json FROM audit_events "
            "WHERE event_type LIKE '%ADOPT%' ORDER BY id DESC LIMIT 500"
        ))
    except sqlite3.OperationalError:
        return ""
    for strategy_id, payload in rows:
        if code in str(payload or ""):
            return str(strategy_id or "")
    return ""


def build_attribution(
    con: sqlite3.Connection,
    campaigns: Iterable[dict],
    tenants: Sequence,
    *,
    primary_tenant,
    overrides: dict[str, str] | None = None,
    positions: dict[str, set[str]] | None = None,
) -> tuple[list[Attribution], dict[str, list[dict]], list[str]]:
    """按判据优先级给每条 campaign 定租户。

    ``positions`` 是**窗口持仓快照**（``{tenant_id: {code}}``，由
    :func:`load_positions_file` 读出）。**给了它就是权威**；没给则退回
    broker_snapshots + 成交流水佐证（仅用于 dry-run 复核）。
    """
    from futu_quant.tenant import resolve_tenant

    overrides = overrides or {}
    positions = positions or {}
    index, unmapped = positions_snapshot_index(con, tenants, primary_tenant=primary_tenant)
    result: list[Attribution] = []

    for campaign in campaigns:
        code = str(campaign.get("code") or campaign.get("campaign_id") or "").strip()
        if not code:
            continue
        tenant_field = str(campaign.get("tenant_id") or "").strip()
        account_field = str(campaign.get("account_id") or "").strip()
        warnings: list[str] = []

        if code in overrides:
            chosen = overrides[code]
            result.append(Attribution(
                code=code, tenant_id=chosen, rule=RULE_OVERRIDE,
                evidence=f"人工指定 --assign {code}={chosen}",
                detail={"override": chosen},
            ))
            continue

        # ① campaign 自带租户/账户
        if tenant_field:
            result.append(Attribution(
                code=code, tenant_id=tenant_field, rule=RULE_FIELD,
                evidence=f"campaign.tenant_id={tenant_field!r}",
                detail={"field": "tenant_id", "value": tenant_field},
            ))
            continue
        if account_field:
            try:
                profile = resolve_tenant(account_id=account_field)
            except Exception:  # noqa: BLE001
                profile = None
            if profile is not None:
                result.append(Attribution(
                    code=code, tenant_id=profile.tenant_id, rule=RULE_FIELD,
                    evidence=f"campaign.account_id={account_field!r} → 注册表 {profile.tenant_id}",
                    detail={"field": "account_id", "value": account_field},
                ))
                continue
            warnings.append(f"campaign.account_id={account_field!r} 无法解析为注册表租户，继续下一判据")

        # ② 净持仓归属
        #    窗口快照（``positions``，由 Ops 在停机窗口内产出）是**权威**；
        #    broker_snapshots 与成交流水是**佐证**。三者冲突时以快照为准并留 WARN。
        net_rows = net_position_by_tenant(con, code)
        snapshot_hits = []
        for t in tenants:
            source = position_codes_for(index, t.tenant_id).get(code)
            if source:
                snapshot_hits.append((t.tenant_id, source))
        window_hits = [t.tenant_id for t in tenants if code in positions.get(t.tenant_id, set())]

        held_by = [tid for tid in window_hits if tid] or [
            r["tenant_id"] for r in net_rows
        ]
        if len(held_by) > 1:
            # 规则 4：两个账户都持有同 code → **不自动裁定**
            warnings.append(
                f"**两个账户都持有 {code}**：{held_by} —— 按裁定不自动选一个，"
                "进待人工确认清单（用 --assign CODE=TENANT_ID 指定）"
            )
            result.append(Attribution(
                code=code, tenant_id="", rule=RULE_FALLBACK,
                evidence=f"双账户持有 {held_by}，无自动裁定规则 → 待人工确认",
                ambiguous=True, warnings=tuple(warnings),
                detail={"held_by": held_by, "net": net_rows, "snapshots": snapshot_hits},
                status=STATUS_PENDING,
            ))
            continue

        if held_by:
            chosen = held_by[0]
            net_part = ""
            for r in net_rows:
                if r["tenant_id"] == chosen:
                    net_part = f"（成交流水净 {r['net']:g}）"
                    break
            corroborated = [t for t, _ in snapshot_hits]
            if corroborated and chosen not in corroborated:
                warnings.append(
                    f"窗口快照指向 {chosen}，但 broker_snapshots 指向 {corroborated} —— "
                    "两者不一致，请人工复核（快照可能过期）"
                )
            result.append(Attribution(
                code=code, tenant_id=chosen, rule=RULE_POSITION,
                evidence=(
                    f"持仓快照显示 {chosen} 持有 {code}{net_part}"
                    + (f"；broker_snapshots 佐证 {corroborated}" if corroborated else "；broker_snapshots 未覆盖")
                ),
                ambiguous=False,
                warnings=tuple(warnings),
                detail={"held_by": held_by, "net": net_rows, "snapshots": snapshot_hits},
            ))
            continue

        if snapshot_hits:
            if len({tid for tid, _ in snapshot_hits}) > 1:
                warnings.append(
                    f"该 code 同时出现在多个账户的 broker_snapshots：{snapshot_hits} —— "
                    "进待人工确认清单"
                )
                result.append(Attribution(
                    code=code, tenant_id="", rule=RULE_FALLBACK,
                    evidence=f"broker_snapshots 显示 {snapshot_hits}，无自动裁定规则 → 待人工确认",
                    ambiguous=True, warnings=tuple(warnings),
                    detail={"snapshots": snapshot_hits}, status=STATUS_PENDING,
                ))
                continue
            tid, source = snapshot_hits[0]
            result.append(Attribution(
                code=code, tenant_id=tid, rule=RULE_POSITION_SNAPSHOT,
                evidence=(
                    f"broker_snapshots 的 {source} 含 {code} → 账户租户 {tid}"
                    "（**窗口快照未覆盖该 code，仅此佐证**）"
                ),
                ambiguous=False,
                warnings=tuple(warnings),
                detail={"snapshots": snapshot_hits},
            ))
            continue

        # ③ 成交意图（有成交但当前无净持仓 —— 说明仓位已平）
        intents = [i for i in intent_tenants(con, code) if i["tenant_id"]]
        if intents:
            ambiguous = len(intents) > 1
            if ambiguous:
                warnings.append(
                    "多租户都有成交意图，按最近意图取租户（平局再比条数）："
                    + "; ".join(
                        f"{i['tenant_id']} 最新 {i['latest']} 共 {i['count']} 条" for i in intents
                    )
                )
            chosen = intents[0]["tenant_id"]
            result.append(Attribution(
                code=code, tenant_id=chosen, rule=RULE_INTENT,
                evidence=(
                    f"order_intents 最新意图属于 {chosen}（当前净持仓为 0，仓位已平）"
                    + (f"（{intents[0]['count']} 条，最新 {intents[0]['latest']}）" if not ambiguous else "")
                ),
                ambiguous=ambiguous,
                warnings=tuple(warnings),
                detail={"intents": intents},
            ))
            continue

        # ④ 两个账户都不持有 —— 按 exit_only + 接管审计推定；推不出就**待人工确认**
        exit_only = bool(campaign.get("exit_only"))
        adopted = _orphan_adopt_source(con, code)
        if exit_only or adopted:
            reason = (
                f"两账户均不持有 {code}；该 campaign exit_only={exit_only}"
                + (f"，接管审计显示来源租户 {adopted}" if adopted else "")
                + f" → 归雷达/主账户租户 {primary_tenant.tenant_id if primary_tenant else ''}"
                "做退出维护；**请人工确认该仓位是否仍在**"
            )
            result.append(Attribution(
                code=code,
                tenant_id=primary_tenant.tenant_id if primary_tenant else "",
                rule=RULE_EXIT_ONLY, evidence=reason, warnings=(reason,),
                detail={"exit_only": exit_only, "adopted_source": adopted},
            ))
            continue

        # ④b：两账户都不持有且推定不出 → **默认归模拟租户**（用户 10-23 裁定）。
        #
        # 判不出来的**绝不判给真金**：模拟盘亏的是虚拟钱、判错可撤；
        # 实盘判错就是真金持仓当场失去管理（10-22 META 的失效模式）。
        #
        # **边界**：没有**启用的非实盘**租户时，就没有"安全的非实盘落点"——
        # 此时**保持待人工确认 + WARN，绝不回落实盘**。宁可拒写，也不默认上真金。
        paper = next(
            (t for t in sorted(tenants, key=lambda x: x.tenant_id)
             if getattr(t, "is_active", False)
             and str(getattr(t, "trading_env", "")).upper() != "REAL"),
            None,
        )
        if paper is not None:
            reason = (
                f"两账户均不持有 {code}，且无 exit_only / 无接管审计可推定 → "
                f"按裁定默认归**模拟租户 {paper.tenant_id}**"
                "（判不出来的绝不判给真金；模拟盘亏的是虚拟钱，判错可撤）"
            )
            warnings.append(reason)
            result.append(Attribution(
                code=code, tenant_id=paper.tenant_id, rule=RULE_FALLBACK,
                evidence=reason, warnings=tuple(warnings),
                detail={"default_to_paper": paper.tenant_id},
            ))
            continue

        reason = (
            f"两账户均不持有 {code}，且无 exit_only / 无接管审计可推定；"
            "**也没有启用的非实盘租户可落** → 保持待人工确认（**绝不回落实盘**）。"
            "用 --assign CODE=TENANT_ID 指定后重跑"
        )
        warnings.append(reason)
        result.append(Attribution(
            code=code, tenant_id="", rule=RULE_FALLBACK, evidence=reason,
            warnings=tuple(warnings),
            detail={"pending": True, "no_paper_tenant": True}, status=STATUS_PENDING,
        ))

    return result, index, unmapped


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


def collect_residual(con: sqlite3.Connection, codes: Sequence[str]) -> dict:
    """这 8 个 code 下**还有没有"正在被管的东西"** —— 清单变空会不会产生孤儿。

    核**两类**东西（Reviewer 要求：只查配置键不够，状态键也算）：

    - **状态键**：``campaign_state`` 表里引用这些 code / 标的的键。它们**同样是全局键、
      无租户维度**（``volatility_long_gamma:{code}``、``volatility_cooldown:{underlying}``、
      ``volatility_long_gamma:signal:{underlying}``、``volatility_expression_review:{underlying}``）。
      本迁移**只搬配置键、不搬状态键** → 清单变空后这些状态键**成为孤儿状态**
      （两租户都不再按它读、也不会被清理）。这是**登记项，不是硬阻断** ——
      处置交给 P0-9（campaign state 写读不对称，@Coder）。
    - **在险仓位**：虚拟持仓 / 未平批次 / 退出分配。
      **硬阻断判据只有一条**：虚拟持仓 remaining_qty != 0 或存在未平批次 → 有真仓位，
      清单变空就是把它变成无主仓位，必须先人工平掉。

    返回 ``{"has_open_risk": bool, ...明细}``。
    """
    codes = [c for c in codes if c]
    empty = {"has_open_risk": False, "campaign_state": [], "state_keys_code": [],
             "state_keys_underlying": [], "orphan_state": False,
             "virtual_positions": [], "open_batches": [], "exit_allocations": []}
    if not codes:
        return empty
    placeholders = ",".join("?" * len(codes))
    underlying = sorted({c.split("2")[0] for c in codes if "2" in c})

    state_keys_code: list[dict] = []
    state_keys_underlying: list[dict] = []
    try:
        for row in con.execute("SELECT state_key, updated_at FROM campaign_state"):
            key = str(row[0])
            if any(code in key for code in codes):
                state_keys_code.append({"key": key, "updated_at": row[1]})
            elif any(u in key for u in underlying):
                state_keys_underlying.append({"key": key, "updated_at": row[1]})
    except sqlite3.OperationalError:
        pass
    campaign_state = state_keys_code + state_keys_underlying
    # 这些状态键本迁移不搬 → 清单变空后即为孤儿状态
    orphan_state = bool(campaign_state)

    virtual_positions = []
    try:
        for row in con.execute(
            "SELECT COALESCE(tenant_id,'') tenant_id, strategy_id, symbol, quantity "
            "FROM virtual_allocations WHERE symbol IN (%s) ORDER BY symbol" % placeholders,
            codes,
        ):
            virtual_positions.append({
                "tenant_id": row[0], "strategy_id": row[1],
                "symbol": row[2], "quantity": row[3],
            })
    except sqlite3.OperationalError:
        pass

    open_batches = []
    try:
        for row in con.execute(
            "SELECT batch_id, strategy_id, campaign_id, symbol, status, remaining_qty "
            "FROM position_batches WHERE symbol IN (%s) ORDER BY batch_id" % placeholders,
            codes,
        ):
            open_batches.append({
                "batch_id": row[0], "strategy_id": row[1], "campaign_id": row[2],
                "symbol": row[3], "status": row[4], "remaining_qty": row[5],
            })
    except sqlite3.OperationalError:
        pass

    exit_allocations = []
    try:
        for row in con.execute(
            "SELECT b.symbol, COUNT(e.id) FROM position_batches b "
            "LEFT JOIN exit_allocations e ON e.batch_id=b.batch_id "
            "WHERE b.symbol IN (%s) GROUP BY b.symbol" % placeholders,
            codes,
        ):
            exit_allocations.append({"symbol": row[0], "exit_rows": row[1]})
    except sqlite3.OperationalError:
        pass

    has_open_risk = any(
        abs(float(p["quantity"] or 0)) > 0 for p in virtual_positions
    ) or any(
        str(b["status"]).upper() != "CLOSED" and abs(float(b["remaining_qty"] or 0)) > 0
        for b in open_batches
    )
    return {
        "has_open_risk": has_open_risk,
        "campaign_state": campaign_state,
        "state_keys_code": state_keys_code,
        "state_keys_underlying": state_keys_underlying,
        "orphan_state": orphan_state,
        "virtual_positions": virtual_positions,
        "open_batches": open_batches,
        "exit_allocations": exit_allocations,
    }


def render_report(
    *,
    attributions: Sequence[Attribution],
    legacy_key: str,
    tenant_keys: dict[str, str],
    legacy_raw: str | None,
    positions_index: dict[str, list[dict]],
    unmapped_snapshots: Sequence[str],
    primary_tenant,
    applied: bool,
    overrides: dict[str, str],
    residual: dict | None = None,
) -> str:
    lines: list[str] = []
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    lines.append("# 波动率 campaign 租户分区迁移报告")
    lines.append("")
    lines.append(f"- 生成时间：{stamp}")
    lines.append(f"- 模式：**{'APPLY（已写库）' if applied else 'DRY-RUN（未写库）'}**")
    lines.append(f"- 无前缀旧键：`{legacy_key}`（**只读 inert，原样保留**）")
    lines.append("- 租户键：" + "、".join(f"`{k}`" for k in tenant_keys.values()))
    lines.append(f"- 雷达/主连接租户（判据④兜底）：`{primary_tenant.tenant_id if primary_tenant else '未解析'}`")
    lines.append("")

    by_tenant: dict[str, list[Attribution]] = {}
    for item in attributions:
        by_tenant.setdefault(item.tenant_id, []).append(item)

    lines.append("## 归属总览")
    lines.append("")
    lines.append("| 租户 | 条数 |")
    lines.append("| :--- | ---: |")
    for tid in sorted(by_tenant):
        lines.append(f"| `{tid}` | {len(by_tenant[tid])} |")
    lines.append(f"| **合计** | **{len(attributions)}** |")
    lines.append("")

    lines.append("## 逐条归属判据")
    lines.append("")
    lines.append("| # | code | 租户 | 判据 | 证据 | 标记 |")
    lines.append("| ---: | :--- | :--- | :--- | :--- | :--- |")
    for i, item in enumerate(attributions, 1):
        marks = []
        if item.pending:
            marks.append("⛔**待人工确认**")
        elif item.rule == RULE_FALLBACK:
            marks.append("**WARN-④b默认归模拟**")
        if item.ambiguous:
            marks.append("AMBIGUOUS")
        for w in item.warnings:
            marks.append("WARN")
        target = item.tenant_id or "**（未分配）**"
        lines.append(
            f"| {i} | `{item.code}` | `{target}` | {item.rule_label} | "
            f"{item.evidence} | {'、'.join(marks) or '—'} |"
        )
    lines.append("")

    warn_items = [i for i in attributions if i.warn]
    lines.append(f"## WARN 审计（{len(warn_items)} 条，必须人工复核）")
    lines.append("")
    if not warn_items:
        lines.append("无。")
    for item in warn_items:
        lines.append(f"- `{item.code}` → `{item.tenant_id or '（未分配）'}`（{item.rule_label}）")
        for w in item.warnings:
            lines.append(f"  - {w}")
    lines.append("")

    lines.append("## 证据来源明细")
    lines.append("")
    lines.append("### 券商持仓快照（判据②）")
    lines.append("")
    if not any(positions_index.values()):
        lines.append("无可用快照。")
    for tid, snaps in sorted(positions_index.items()):
        if not snaps:
            lines.append(f"- `{tid}`：**无快照**")
            continue
        parts = [f"`{s['key']}`@{s['captured_at']}" for s in snaps]
        lines.append(f"- `{tid}`：{'; '.join(parts)}")
    if unmapped_snapshots:
        lines.append("")
        lines.append(
            "- ⚠️ **未能映射到租户的快照**（不参与判据②，避免误判）："
            + "、".join(f"`{k}`" for k in unmapped_snapshots)
        )
    lines.append("")

    lines.append("### 成交意图（判据③，按租户）")
    lines.append("")
    for item in attributions:
        intents = item.detail.get("intents")
        if intents:
            lines.append(
                f"- `{item.code}`："
                + "；".join(
                    f"{i['tenant_id']} 最新 {i['latest']} 共 {i['count']} 条" for i in intents
                )
            )
    lines.append("")

    lines.append("## 写入计划")
    lines.append("")
    for tid in sorted(by_tenant):
        codes = [i.code for i in by_tenant[tid]]
        if not tid:
            lines.append(
                f"- **未分配（待人工确认）** ← {len(codes)} 条："
                + ", ".join(f"`{c}`" for c in codes)
            )
            continue
        lines.append(f"- `{tenant_keys[tid]}` ← {len(codes)} 条：{', '.join('`%s`' % c for c in codes)}")
    empty = [tid for tid, key in tenant_keys.items() if tid not in by_tenant]
    for tid in empty:
        lines.append(
            f"- `{tenant_keys[tid]}` ← **0 条（写空列表，表示该租户确实没有 campaign）**"
        )
    lines.append("")

    lines.append("## 设计口径 vs 实测（差异留痕）")
    lines.append("")
    lines.append(
        "- 设计文档口径（`docs/volatility_campaigns_tenant_partition_design.md`）："
        f"{DESIGN_DOC_EXPECTED_CAMPAIGNS} 条"
    )
    lines.append(f"- 本次实测无前缀旧键 `{legacy_key}`：{len(attributions)} 条")
    if len(attributions) == DESIGN_DOC_EXPECTED_CAMPAIGNS:
        lines.append("- 一致，无差异。")
    else:
        diff = DESIGN_DOC_EXPECTED_CAMPAIGNS - len(attributions)
        lines.append(
            f"- ⚠️ **设计 {DESIGN_DOC_EXPECTED_CAMPAIGNS} 条 / 实测 {len(attributions)} 条"
            f"（相差 {abs(diff)} 条，设计 - 实测 = {diff}）**：设计期假设值与磁盘不符，"
            "原因待 Architect 核对；**不得默默用实测值抹掉设计口径** —— 差异必须留痕。"
        )
    lines.append("")

    lines.append("## 不变量自检")
    lines.append("")
    lines.append(f"- 无前缀旧键 `{legacy_key}`：**只读，迁移前后逐字节不变**")
    if legacy_raw is not None:
        lines.append(f"  - 迁移前长度：{len(legacy_raw)} 字节；SHA256：`{hashlib.sha256(legacy_raw.encode()).hexdigest()}`")
    lines.append("- 不触碰 `order_intents` / `fill_ledger` / `broker_snapshots` / 任何持仓与账本")
    if overrides:
        lines.append(f"- 人工指定覆盖：{json.dumps(overrides, ensure_ascii=False)}")
    lines.append("")

    if residual is not None:
        lines.append("## 模拟租户残留核查（Chief 放行前置条件）")
        lines.append("")
        lines.append(
            "问题：这 8 条现在**两个租户 Worker 都在读**（同一份全局清单）。"
            "模拟租户清单一旦变空，它名下挂的东西会不会变成孤儿？"
        )
        lines.append("")
        if residual["has_open_risk"]:
            lines.append(
                "### ⛔ 硬阻断：存在**未平**的虚拟持仓或批次 —— 禁止 `--apply`**"
            )
            lines.append("")
            lines.append("清单变空 = 把这些仓位变成无主仓位，必须先人工平掉。")
        else:
            lines.append("### ✅ 无未平风险：虚拟持仓与批次都是空的，清单变空不产生孤儿")
        lines.append("")

        lines.append(f"**① 状态键 `campaign_state`（共 {len(residual['campaign_state'])} 条，全局键、无租户维度）**")
        lines.append("")
        lines.append("**①a 引用 code 的**"
                     f"（{len(residual['state_keys_code'])} 条）")
        lines.append("")
        if not residual["state_keys_code"]:
            lines.append("- 无。")
        for item in residual["state_keys_code"]:
            lines.append(f"- `{item['key']}`（{item['updated_at']}）")
        lines.append("")
        lines.append("**①b 引用标的 underlying 的**"
                     f"（{len(residual['state_keys_underlying'])} 条）")
        lines.append("")
        if not residual["state_keys_underlying"]:
            lines.append("- 无。")
        for item in residual["state_keys_underlying"]:
            lines.append(f"- `{item['key']}`（{item['updated_at']}）")
        lines.append("")
        if residual["orphan_state"]:
            lines.append(
                "**⚠️ 孤儿状态判定：会。** 本迁移**只搬配置键**（`VOLATILITY_CAMPAIGNS:{tenant}`），"
                "**不搬状态键**。这些状态键是**全局键、无租户维度**，"
                "两租户的 Worker 读的是同一份；模拟租户清单变空后，它们**不再被任何一方按租户读取**，"
                "也不会被 `prune_campaign_states` 清理（它只清 `volatility_long_gamma:%` 里"
                "不在 `active_keys` 的键）。"
            )
            lines.append("")
            lines.append(
                "**处置建议（不在本脚本写入范围内，交给 P0-9 / @Coder）**：\n"
                "1. 状态键本身也要租户化（`volatility_long_gamma:{tenant}:{code}` 形态），"
                "与 P0-9 的 `active_keys` 写读配对同批修；\n"
                "2. 在修好之前，**不要删这些状态键** —— 删了会让两个租户同时丢冷却/信号状态；\n"
                "3. 本迁移**不删也不搬**它们，只在此登记。"
            )
        else:
            lines.append("- 无状态键命中。")
        lines.append("")
        lines.append(
            "> 注：`volatility_long_gamma:{code}` 等形态**与 P0-9 是同一个根因** —— "
            "`order_push.py:157` 写带租户的键、`run_volatility_strategy.py:213-221` 只认不带租户的键。"
            "本迁移不处理，见 P0-9。"
        )
        lines.append("")

        lines.append("**② 虚拟持仓 `virtual_allocations`（命中本批 code 的行）**")
        lines.append("")
        if not residual["virtual_positions"]:
            lines.append("- 无命中行。")
        for p in residual["virtual_positions"]:
            flag = "**非零，有风险**" if abs(float(p["quantity"] or 0)) > 0 else "qty=0（无在险）"
            lines.append(
                f"- `{p['symbol']}` strategy=`{p['strategy_id']}` tenant_id=`{p['tenant_id']!r}` "
                f"qty={p['quantity']} → {flag}"
            )
        lines.append("")

        lines.append("**③ 未平批次 `position_batches`**")
        lines.append("")
        if not residual["open_batches"]:
            lines.append("- 无命中行。")
        for b in residual["open_batches"]:
            flag = "**未平，有风险**" if abs(float(b["remaining_qty"] or 0)) > 0 else "已平"
            lines.append(
                f"- batch=`{b['batch_id']}` symbol=`{b['symbol']}` status=`{b['status']}` "
                f"remaining={b['remaining_qty']} → {flag}"
            )
        lines.append("")

        lines.append("**④ 退出分配 `exit_allocations`**")
        lines.append("")
        if not residual["exit_allocations"]:
            lines.append("- 无命中行。")
        for e in residual["exit_allocations"]:
            lines.append(f"- `{e['symbol']}` 关联退出记录 {e['exit_rows']} 条")
        lines.append("")

    pending = [i for i in attributions if i.pending]
    if pending:
        lines.append("## ⛔ 待人工确认（**不猜**，apply 会被拒绝）")
        lines.append("")
        lines.append("| code | 现状 | 原因 |")
        lines.append("| :--- | :--- | :--- |")
        for item in pending:
            lines.append(f"| `{item.code}` | **未分配** | {item.evidence} |")
        lines.append("")
        lines.append(
            "处置：用 `--assign CODE=TENANT_ID` 逐条指定后重跑。"
            "**本迁移不会替你选一个** —— 猜错就是让某个租户的真实持仓当场失去管理。"
        )
        lines.append("")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 备份
# --------------------------------------------------------------------------


def check_free_space(path: Path, min_free_gb: float) -> int:
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(str(path)).free
    if free < int(min_free_gb * 1024 ** 3):
        raise RuntimeError(
            f"磁盘余量不足：{path} 可用 {free / 1024 ** 3:.2f} GB，要求 ≥ {min_free_gb:g} GB"
            f"（trading.db 实测约 {DB_SIZE_BYTES / 1024 ** 3:.1f} GB，备份需要等量空间）。"
            "清理磁盘后重试 —— **绝不带着不足的空间做写迁移**。"
        )
    return free


def backup_database(db_path: Path, backup_dir: Path, min_free_gb: float) -> dict:
    """用 SQLite 在线备份 API 备份，并回读校验。"""
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
            dst.close()
        except Exception:
            dst.close()
            raise
    finally:
        src.close()

    verify = sqlite3.connect(str(dest))
    try:
        rows_before = verify.execute("SELECT COUNT(*) FROM runtime_config").fetchone()[0]
        settings_rows = verify.execute("SELECT COUNT(*) FROM system_settings").fetchone()[0]
    finally:
        verify.close()

    digest = hashlib.sha256()
    with dest.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    manifest = {
        "source": str(db_path),
        "backup": str(dest),
        "size_bytes": dest.stat().st_size,
        "sha256": digest.hexdigest(),
        "page_count": page_count,
        "quick_check": quick,
        "runtime_config_rows": rows_before,
        "system_settings_rows": settings_rows,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    (dest.with_suffix(dest.suffix + ".manifest.json")).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return manifest


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="真的写库（默认 dry-run）")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB)
    parser.add_argument("--skip-backup", action="store_true", help="跳过备份（仅在已确认有可用备份时）")
    parser.add_argument("--report", type=Path, default=None, help="报告落盘路径")
    parser.add_argument(
        "--assign", action="append", default=[], metavar="CODE=TENANT_ID",
        help="人工指定归属，可重复（优先级最高）",
    )
    parser.add_argument(
        "--positions-file", type=Path, default=None,
        help=(
            "窗口持仓快照 JSON（Ops 在停机窗口内产出）。"
            "**--apply 时强烈建议提供**：脚本没有 broker，缺了它只能靠 broker_snapshots "
            "与成交流水佐证，可能漏掉快照未覆盖的真实持仓"
        ),
    )
    parser.add_argument(
        "--max-snapshot-age-minutes", type=float, default=30.0,
        help="持仓快照最大允许年龄（分钟），超过即 fail-closed 拒绝 apply（默认 30）",
    )
    parser.add_argument("--json", action="store_true", help="额外输出机器可读 JSON 摘要")
    return parser.parse_args(argv)


def _parse_overrides(raw: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"--assign 需要 CODE=TENANT_ID 形式，收到 {item!r}")
        code, tid = item.split("=", 1)
        code, tid = code.strip(), tid.strip()
        if not code or not tid:
            raise SystemExit(f"--assign 需要 CODE=TENANT_ID 形式，收到 {item!r}")
        out[code] = tid
    return out


def run(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from futu_quant.store import TradingStore
    from futu_quant.tenant import DEFAULT_TENANTS

    tenants = [t for t in DEFAULT_TENANTS.values()]
    active = [t for t in tenants if t.is_active]
    overrides = _parse_overrides(args.assign)

    for tid in overrides.values():
        if tid not in DEFAULT_TENANTS:
            raise SystemExit(f"--assign 指定的租户 {tid!r} 不在注册表 {sorted(DEFAULT_TENANTS)}")

    primary_tenant = resolve_primary_tenant(active)
    legacy = legacy_config_key(VOLATILITY_CAMPAIGNS_KEY)
    tenant_keys = {t.tenant_id: campaigns_config_key(t.tenant_id) for t in tenants}

    db_path = args.db.resolve()
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT value_json FROM runtime_config WHERE config_key=?", (legacy,)
    ).fetchone()
    legacy_raw = row["value_json"] if row else None
    campaigns = json.loads(legacy_raw) if legacy_raw else []

    print(f"[迁移] 库：{db_path}")
    print(f"[迁移] 无前缀旧键 {legacy}：{len(campaigns)} 条 campaign")
    if len(campaigns) != DESIGN_DOC_EXPECTED_CAMPAIGNS:
        diff = DESIGN_DOC_EXPECTED_CAMPAIGNS - len(campaigns)
        _print_safe(
            f"[迁移] ⚠️ 口径差异：设计 {DESIGN_DOC_EXPECTED_CAMPAIGNS} 条 / 实测 {len(campaigns)} 条"
            f"（相差 {abs(diff)} 条，设计 - 实测 = {diff}），待 Architect 核对，"
            "**不得默默用实测值抹掉设计口径**"
        )
    print(f"[迁移] 租户注册表：{[t.tenant_id for t in tenants]}（启用 {sorted(t.tenant_id for t in active)}）")
    print(f"[迁移] 雷达/主连接租户（判据④）：{primary_tenant.tenant_id if primary_tenant else '未解析'}")
    print(f"[迁移] 模式：{'APPLY' if args.apply else 'DRY-RUN'}")
    print()

    # 窗口持仓快照（Ops 产出）—— 给了就是权威判据
    window_positions: dict[str, set[str]] = {}
    if args.positions_file is not None:
        window_positions = load_positions_file(
            args.positions_file, tenants=tenants,
            max_age_minutes=args.max_snapshot_age_minutes,
        )
        print(f"[迁移] 持仓快照：{args.positions_file}")
        for tid in sorted(window_positions):
            n = len(window_positions[tid])
            print(f"  {tid}: {n} 个非零持仓 code")
        print()
    elif args.apply:
        _print_safe("[迁移] ⚠️ 未提供 --positions-file —— apply 将只靠 broker_snapshots / 成交流水佐证。")
        print("       快照可能未覆盖真实持仓（10-23 实测：模拟的 QQQ +1 不在快照里）。")
        print("       强烈建议由 Ops 产出快照后带 --positions-file 重跑。")
        print()

    attributions, positions_index, unmapped = build_attribution(
        con, campaigns, tenants, primary_tenant=primary_tenant, overrides=overrides,
        positions=window_positions,
    )
    codes = [a.code for a in attributions]
    residual = collect_residual(con, codes)
    con.close()

    # 归属冲突自检：同一 code 出现多次（注册表里可能重复）
    codes = [a.code for a in attributions]
    duplicates = sorted({c for c in codes if codes.count(c) > 1})
    if duplicates:
        print(f"[迁移][ERROR] 同一 code 在旧键里出现多次，先清理再迁移：{duplicates}")
        return 2

    print("[迁移] 逐条归属：")
    for i, item in enumerate(attributions, 1):
        if item.pending:
            flag = " ⛔待人工确认"
            target = "（未分配）"
        else:
            flag = " ⚠️WARN" if item.warn else ""
            target = item.tenant_id
        print(f"  {i:2d}. {item.code:<26} → {target:<22} [{item.rule_label}]{flag}")
        print(f"      证据：{item.evidence}")
        for w in item.warnings:
            print(f"      警告：{w}")
    print()

    by_tenant: dict[str, list[Attribution]] = {}
    for item in attributions:
        by_tenant.setdefault(item.tenant_id, []).append(item)
    for tid in sorted(tenant_keys):
        n = len(by_tenant.get(tid, []))
        print(f"[迁移] 计划写入 {tenant_keys[tid]} ← {n} 条")
    pending_items = [i for i in attributions if i.pending]
    if pending_items:
        _print_safe(f"[迁移] ⛔ {len(pending_items)} 条**待人工确认**（未分配，apply 会被拒绝）："
              + ", ".join(f"`{i.code}`" for i in pending_items))
    print()

    print("[迁移] 模拟租户残留核查：")
    print(f"  状态键（campaign_state）命中 {len(residual['campaign_state'])} 条（全局键，无租户维度）："
          f" 引用 code {len(residual['state_keys_code'])} 条 / 引用标的 {len(residual['state_keys_underlying'])} 条")
    _print_safe(f"  → 孤儿状态：{'⚠️ 会（本迁移只搬配置键、不搬状态键，处置交 P0-9）' if residual['orphan_state'] else '无'}")
    print(f"  虚拟持仓命中 {len(residual['virtual_positions'])} 行"
          f"（非零 {sum(1 for p in residual['virtual_positions'] if abs(float(p['quantity'] or 0)) > 0)} 行）")
    print(f"  未平批次 {len(residual['open_batches'])} 行")
    print(f"  退出分配 {len(residual['exit_allocations'])} 行")
    _print_safe(f"  → {'⛔ 存在未平风险，禁止 --apply' if residual['has_open_risk'] else '✅ 无未平风险'}")
    print()

    if residual["has_open_risk"] and args.apply:
        print("[迁移][ERROR] 残留核查发现未平的虚拟持仓/批次 —— 清单变空会制造无主仓位。")
        print("             先人工平掉这些仓位，再重跑 --apply。")
        return 5

    report_path = args.report or (
        args.backup_dir
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_volatility_campaigns_tenant_scope_"
          f"{'apply' if args.apply else 'dryrun'}.md"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(
            attributions=attributions,
            legacy_key=legacy,
            tenant_keys=tenant_keys,
            legacy_raw=legacy_raw,
            positions_index=positions_index,
            unmapped_snapshots=unmapped,
            primary_tenant=primary_tenant,
            applied=False,
            overrides=overrides,
            residual=residual,
        ),
        encoding="utf-8",
    )
    print(f"[迁移] 报告已落盘：{report_path}")

    if not args.apply:
        if pending_items:
            print()
            _print_safe(f"[迁移] ⛔ {len(pending_items)} 条待人工确认 —— 用 --assign CODE=TENANT_ID 指定后重跑。")
        print()
        print("[迁移] DRY-RUN 结束，未写库。复核报告后：停 Worker → --apply → 立刻 start。")
        return 0

    # ---------------- APPLY ----------------
    if pending_items:
        print()
        print(f"[迁移][ERROR] 有 {len(pending_items)} 条待人工确认 —— **拒绝 apply**（不猜）：")
        for item in pending_items:
            print(f"  - `{item.code}`：{item.evidence}")
        print()
        print("处置：用 --assign CODE=TENANT_ID 逐条指定后重跑。")
        return 6

    if args.json:
        print(json.dumps({
            "legacy_key": legacy,
            "tenant_keys": tenant_keys,
            "primary_tenant": primary_tenant.tenant_id if primary_tenant else None,
            "count": len(attributions),
            "by_tenant": {tid: [i.code for i in items] for tid, items in by_tenant.items()},
            "residual": {
                "has_open_risk": residual["has_open_risk"],
                "orphan_state": residual["orphan_state"],
                "state_keys_code": len(residual["state_keys_code"]),
                "state_keys_underlying": len(residual["state_keys_underlying"]),
                "campaign_state": len(residual["campaign_state"]),
                "virtual_positions": residual["virtual_positions"],
                "open_batches": residual["open_batches"],
                "exit_allocations": residual["exit_allocations"],
            },
            "warnings": [
                {"code": i.code, "tenant_id": i.tenant_id, "rule": i.rule, "warnings": list(i.warnings)}
                for i in attributions if i.warn
            ],
            "pending": [
                {"code": i.code, "evidence": i.evidence} for i in attributions if i.pending
            ],
        }, ensure_ascii=False, indent=2))

    # ---------------- APPLY ----------------
    print()
    if not args.skip_backup:
        manifest = backup_database(db_path, args.backup_dir, args.min_free_gb)
        print(f"[迁移] 备份完成：{manifest['backup']}")
        print(f"       大小 {manifest['size_bytes'] / 1024 ** 3:.2f} GB，"
              f"page_count={manifest['page_count']}，quick_check={manifest['quick_check']}")
        print(f"       SHA256 {manifest['sha256']}")
    else:
        _print_safe("[迁移] ⚠️ 已跳过备份（--skip-backup）—— 请确认已有可用备份")

    store = TradingStore(db_path)

    # 写入前再读一次旧键，确保备份之后没有别人改过它
    before = store.get_runtime_config(legacy)
    if before is None:
        print(f"[迁移][ERROR] 旧键 {legacy} 在写入前消失了，中止")
        return 3

    written = {}
    for tid, key in tenant_keys.items():
        codes = {a.code for a in by_tenant.get(tid, [])}
        value = [dict(c) for c in campaigns if str(c.get("code") or "") in codes]
        store.set_runtime_config(key, value, "migrate_volatility_campaigns_tenant_scope")
        written[key] = len(value)
        print(f"[迁移] 已写 {key} ← {len(value)} 条")

    # 不变量：旧键逐字节不变
    after_raw = sqlite3.connect(str(db_path)).execute(
        "SELECT value_json FROM runtime_config WHERE config_key=?", (legacy,)
    ).fetchone()
    after_text = after_raw[0] if after_raw else None
    if after_text != legacy_raw:
        # 不变量被破坏 → 回滚本次写入的租户键（旧键本身不是本脚本改的，
        # 但它变了就说明有并发写入，本次迁移的快照不可信）
        print("[迁移][ERROR] 无前缀旧键内容发生了变化 —— 回滚本次写入的租户键！")
        try:
            with store.connection() as con:
                con.executemany(
                    "DELETE FROM runtime_config WHERE config_key=?",
                    [(key,) for key in tenant_keys.values()],
                )
            print(f"[迁移] 已删除本次写入的 {len(tenant_keys)} 个租户键。")
        except Exception as exc:  # noqa: BLE001
            print(f"[迁移][ERROR] 回滚失败，请用备份还原：{exc}")
        return 4
    print(f"[迁移] 不变量通过：{legacy} 逐字节不变（{len(legacy_raw or '')} 字节）")

    # WARN 审计
    for item in attributions:
        if not item.warn:
            continue
        store.audit("VOLATILITY_CAMPAIGN_TENANT_INFERRED", {
            "code": item.code,
            "tenant_id": item.tenant_id,
            "rule": item.rule,
            "evidence": item.evidence,
            "ambiguous": item.ambiguous,
            "warnings": list(item.warnings),
            "legacy_key": legacy,
            "tenant_key": tenant_keys.get(item.tenant_id),
        }, "volatility_scanner", actor="cli:migrate_campaign_tenant_scope")
    print(f"[迁移] WARN 审计已写入 {sum(1 for i in attributions if i.warn)} 条")

    final = render_report(
        attributions=attributions,
        legacy_key=legacy,
        tenant_keys=tenant_keys,
        legacy_raw=legacy_raw,
        positions_index=positions_index,
        unmapped_snapshots=unmapped,
        primary_tenant=primary_tenant,
        applied=True,
        overrides=overrides,
        residual=residual,
    )
    report_path.write_text(final, encoding="utf-8")
    print(f"[迁移] APPLY 报告已覆盖落盘：{report_path}")
    print()
    _print_safe("[迁移] ⚠️ 立刻用新代码 start（中间不得留旧 Worker，否则旧代码会再写无前缀键）")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
