from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import os
import re
from typing import Iterable

import pandas as pd

from .option_lifecycle import parse_option_code
from .tenant import campaigns_config_key_lenient, tenant_of_broker


FILLED_STATUSES = {"FILLED", "FILLED_ALL", "SHADOW_FILLED"}
OPEN_ORDER_MARKERS = ("待", "提交", "未成交", "SUBMITTED", "SUBMIT", "FILLED_PART")


@dataclass(frozen=True)
class VolatilityOrphanPosition:
    code: str
    underlying: str
    quantity: int
    can_sell_quantity: int
    cost_price: float
    nominal_price: float
    market_value: float
    pnl_value: float
    pnl_ratio: float
    strategy_net_quantity: float
    pending_order_count: int
    status: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def option_codes_from_campaigns(campaigns: Iterable[dict]) -> set[str]:
    codes: set[str] = set()
    for campaign in campaigns:
        code = str(campaign.get("code") or "").upper()
        if parse_option_code(code):
            codes.add(code)
        for leg in campaign.get("legs") or []:
            leg_code = str(leg.get("code") or leg.get("symbol") or "").upper()
            if parse_option_code(leg_code):
                codes.add(leg_code)
    return codes


def _number(value: object, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def retain_campaign_if_at_risk(
    campaign: dict,
    positions: pd.DataFrame,
    orders: pd.DataFrame,
    broker=None,
) -> dict | None:
    """共享不变量：**持有券商持仓或挂单的 campaign 永不被删除，只降级为 exit_only**。

    这是产品级不变量（10-22 事故的根因之一）：雷达与 worker 两条滚动路径都会
    「重选表达」并用新合约替换 campaign。若被替换的旧合约仍有在险头寸，替换动作
    会让该仓位瞬间失去归属 —— 而止损评估只遍历 campaign 列表，于是仓位进入
    「无保护窗口」，直到下一个周期的孤儿检测器补登记。

    两条滚动路径（worker `run_volatility_strategy` 与雷达 `volatility_scanner`）
    都必须先调用本函数：返回非 None 表示该 campaign 仍在险，调用方必须保留它为
    `exit_only` 而不是删除。

    返回保留用的 campaign dict（含 `exit_only=True` 与替换说明）；无在险头寸时返回 None。
    """
    code = str(campaign.get("code") or "").upper()
    if not code:
        return None
    has_position = False
    if positions is not None and not positions.empty and "code" in positions.columns:
        rows = positions[positions["code"].astype(str).str.upper() == code]
        if not rows.empty:
            has_position = (
                pd.to_numeric(rows.get("qty"), errors="coerce").fillna(0).abs().sum() > 0
            )
    has_pending = False
    if orders is not None and not orders.empty and {"code", "order_status"}.issubset(orders.columns):
        order_rows = orders[orders["code"].astype(str).str.upper() == code]
        has_pending = bool(
            not order_rows.empty
            and order_rows["order_status"].map(broker.is_cancellable_status).any()
        ) if broker is not None else bool(not order_rows.empty)
    if not (has_position or has_pending):
        return None
    return {
        **campaign,
        "exit_only": True,
        "retained_reason": (
            "持有券商持仓" if has_position else "存在未成交挂单"
        ) + "，保留为退出维护配置，不得删除",
    }


def protection_coverage(
    positions: pd.DataFrame,
    campaigns: Iterable[dict],
) -> tuple[float, list[str]]:
    """保护覆盖率：任一时刻，可解析的期权券商持仓都必须有归属（在 campaign 内或已被 ADOPT）。

    背景：10-22 事故的缺口是**静默**的 —— 仓位失去归属后没有任何指标反映，
    只能靠人工翻日志发现。本指标把「缺口」变成可观测、可告警的量。

    返回 ``(覆盖率, 未被覆盖的合约代码列表)``；覆盖率 < 1.0 即存在无保护仓位。
    """
    codes = option_codes_from_campaigns(list(campaigns))
    uncovered: list[str] = []
    if positions is None or positions.empty or "code" not in positions.columns:
        return 1.0, uncovered
    for code in positions["code"].astype(str).str.upper().unique():
        if not parse_option_code(code):
            continue
        qty = pd.to_numeric(
            positions[positions["code"].astype(str).str.upper() == code].get("qty"),
            errors="coerce",
        ).fillna(0)
        if qty.abs().sum() <= 0:
            continue
        if code not in codes:
            uncovered.append(code)
    total = len({c for c in positions["code"].astype(str).str.upper().unique() if parse_option_code(c)})
    if total <= 0:
        return 1.0, uncovered
    return round((total - len(uncovered)) / total, 6), uncovered


def _strategy_net_quantities(store, strategy_id: str, limit: int = 50000) -> dict[str, float]:
    net: dict[str, float] = {}
    for intent in store.intents(limit=limit):
        if str(intent.get("strategy_id")) != strategy_id:
            continue
        status = str(intent.get("status", "")).upper()
        filled = _number(intent.get("filled_quantity"))
        if filled <= 0 and status not in FILLED_STATUSES:
            continue
        quantity = filled if filled > 0 else _number(intent.get("quantity"))
        if quantity <= 0:
            continue
        symbol = str(intent.get("symbol") or "").upper()
        if parse_option_code(symbol) is None:
            continue
        side = str(intent.get("side", "")).upper()
        if side == "BUY":
            net[symbol] = net.get(symbol, 0.0) + quantity
        elif side == "SELL":
            net[symbol] = net.get(symbol, 0.0) - quantity
    return net


def _latest_strategy_buy(store, strategy_id: str, code: str) -> dict | None:
    for intent in store.intents(limit=50000):
        if (
            str(intent.get("strategy_id")) == strategy_id
            and str(intent.get("symbol") or "").upper() == code.upper()
            and str(intent.get("side", "")).upper() == "BUY"
            and _number(intent.get("filled_quantity")) > 0
        ):
            return intent
    return None


def _pending_counts(orders: pd.DataFrame | None, broker) -> dict[str, int]:
    if orders is None or orders.empty or "code" not in orders.columns:
        return {}
    if "order_status" not in orders.columns:
        return {
            str(code).upper(): int(count)
            for code, count in orders["code"].astype(str).str.upper().value_counts().items()
        }
    counts: dict[str, int] = {}
    for _, row in orders.iterrows():
        status = row.get("order_status")
        try:
            active = bool(broker.is_cancellable_status(status))
        except Exception:
            active = any(marker in str(status).upper() for marker in OPEN_ORDER_MARKERS)
        if not active:
            continue
        code = str(row.get("code") or "").upper()
        counts[code] = counts.get(code, 0) + 1
    return counts


def detect_volatility_orphan_positions(
    store,
    positions: pd.DataFrame | None,
    orders: pd.DataFrame | None,
    broker,
    campaigns: Iterable[dict],
    strategy_id: str = "volatility_long_gamma",
) -> list[VolatilityOrphanPosition]:
    """Find broker option positions created by the strategy but no longer managed.

    The detector is intentionally conservative: a broker position is considered
    an orphan only when the same option has a positive net filled quantity in
    this strategy's order-intent ledger. This avoids selling manually created
    option positions simply because their underlying is also monitored.
    """
    if positions is None or positions.empty or "code" not in positions.columns:
        return []
    campaign_codes = option_codes_from_campaigns(campaigns)
    states = store.campaign_states()
    strategy_net = _strategy_net_quantities(store, strategy_id)
    pending = _pending_counts(orders, broker)
    rows: list[VolatilityOrphanPosition] = []
    for _, row in positions.iterrows():
        code = str(row.get("code") or "").upper()
        contract = parse_option_code(code)
        if contract is None:
            continue
        quantity = int(_number(row.get("qty", row.get("quantity", 0))))
        if quantity <= 0:
            continue
        if code in campaign_codes:
            continue
        state = states.get(f"{strategy_id}:{code}") or {}
        if int(_number(state.get("previous_position_quantity"))) > 0:
            continue
        strategy_net_quantity = strategy_net.get(code, 0.0)
        if strategy_net_quantity <= 0:
            continue
        can_sell = int(_number(row.get("can_sell_qty", row.get("sellable_qty", quantity))))
        pending_count = pending.get(code, 0)
        status = (
            "PENDING_EXIT_ORDER" if pending_count else
            "SELLABLE_ORPHAN" if can_sell > 0 else
            "LOCKED_ORPHAN"
        )
        reason = (
            "富途存在由波动率策略买入的期权持仓，但当前策略配置和状态均未接管"
        )
        rows.append(VolatilityOrphanPosition(
            code=code,
            underlying=contract.underlying,
            quantity=quantity,
            can_sell_quantity=can_sell,
            cost_price=_number(row.get("cost_price")),
            nominal_price=_number(row.get("nominal_price", row.get("last_price"))),
            market_value=_number(row.get("market_val", row.get("market_value"))),
            pnl_value=_number(row.get("pl_val", row.get("pnl_value"))),
            pnl_ratio=_number(row.get("pl_ratio", row.get("pnl_ratio"))),
            strategy_net_quantity=strategy_net_quantity,
            pending_order_count=pending_count,
            status=status,
            reason=reason,
        ))
    return rows


def volatility_write_state_key(
    code: str, tenant_id: str = "", prefix: str = "volatility_long_gamma:",
) -> str:
    """状态键的**写形态** —— 全系统唯一出处，禁止各处手拼。

    #1 键级分区的状态键半边。此前四个读写点用了**三种互不兼容的格式**：

    * ``order_push`` 写 ``volatility_long_gamma:{tenant}:{code}``（带租户）
    * ``volatility_orphans`` 写 ``volatility_long_gamma:{code}``（不带）
    * ``run_gamma_strategy`` 写 ``{strategy_id}:{code}``（不带）
    * ``run_service`` 按租户读

    后果：迁移 A 之后清单搬到租户键，而 ``volatility_orphans`` / ``run_gamma_strategy``
    写的状态**读侧看不到** —— 存量全局状态键全部变孤儿，
    ``peak`` / ``position_opened_at`` / ``entry_iv_actual`` 这些**止盈止损基准当场丢失**
    （违反「写侧必须是读侧的超集」）。

    **同一个 code 写两种键 = 同一份状态裂成两半**，比不分区更糟。所以写形态收敛到这里，
    四个点都调它。``tenant_id`` 为空时退回不带租户的形态（等价迁移前行为），
    但**生产路径一定会传**（各调用点用 ``tenant_of_broker``）。
    """
    code = str(code or "").strip()
    tenant_id = str(tenant_id or "").strip()
    return f"{prefix}{tenant_id}:{code}" if tenant_id else f"{prefix}{code}"


SIGNAL_STATE_SEGMENT = "signal"
#: 标的形态（`US.AMD` / `US.BRK.B`）。信号键的第三段必须是它，否则不是信号键。
_UNDERLYING_PATTERN = re.compile(r"^US\.[A-Z0-9.]+$")


def volatility_signal_state_key(
    underlying: str, tenant_id: str, prefix: str = "volatility_long_gamma:",
) -> str:
    """**规范**信号状态键：`{strategy}:signal:{tenant}:{UNDERLYING}`（2026-10-27 加租户段）。

    为什么要租户段：信号状态（确认计时）原来只有一条无租户键，实盘与模拟两个 Worker
    共用 → 一旦同标的出现，两边会互相推进对方确认计时。加租户段后各租户独立。
    **租户缺失即 fail-closed**（抛错），绝不静默回落到共享键。
    """
    normalized_underlying = str(underlying or "").strip().upper()
    normalized_tenant = str(tenant_id or "").strip()
    if not normalized_underlying or not normalized_tenant:
        raise ValueError(
            "信号状态键必须同时提供标的与租户（fail-closed：禁止无租户共享键）"
        )
    return f"{prefix}{SIGNAL_STATE_SEGMENT}:{normalized_tenant}:{normalized_underlying}"


def legacy_volatility_signal_state_key(
    underlying: str, prefix: str = "volatility_long_gamma:",
) -> str:
    """历史（无租户段）信号键：`{strategy}:signal:{UNDERLYING}`，只用于迁移。**

    读侧认它、写侧只写规范键；迁移是「只搬不复制」（搬到规范键后删旧键）。
    """
    return f"{prefix}{SIGNAL_STATE_SEGMENT}:{str(underlying or '').strip().upper()}"


def parse_signal_state_key(
    key: str, prefix: str = "volatility_long_gamma:",
) -> tuple[str | None, str | None]:
    """**严格解析**信号状态键，返回 ``(tenant_id, underlying)``；非法 → ``(None, None)``。

    只接受两种形态（过滤/剪枝共用本函数，不许各写一套）：
      - 规范：`volatility_long_gamma:signal:{tenant}:{US.XXX}` → (tenant, US.XXX)
      - 历史：`volatility_long_gamma:signal:{US.XXX}`       → ("", US.XXX)
    反例（一律非法）：缺标的、多段、非 `signal` 段、非波动率前缀、标的不是 `US.*`。
    """
    if not key or not key.startswith(prefix):
        return None, None
    segments = key[len(prefix):].split(":")
    if len(segments) == 2:
        if segments[0] != SIGNAL_STATE_SEGMENT:
            return None, None
        underlying = segments[1].strip().upper()
        if not _UNDERLYING_PATTERN.match(underlying):
            return None, None
        return "", underlying
    if len(segments) == 3:
        if segments[0] != SIGNAL_STATE_SEGMENT or not segments[1].strip():
            return None, None
        underlying = segments[2].strip().upper()
        if not _UNDERLYING_PATTERN.match(underlying):
            return None, None
        return segments[1].strip(), underlying
    return None, None


def is_signal_state_key(key: str, prefix: str = "volatility_long_gamma:") -> bool:
    """是不是合法信号状态键（含历史无租户形态）。过滤/剪枝/测试共用。"""
    _, underlying = parse_signal_state_key(key, prefix)
    return underlying is not None


def is_legacy_signal_state_key(key: str, prefix: str = "volatility_long_gamma:") -> bool:
    """是不是**历史无租户段**的信号状态键：`{strategy}:signal:{UNDERLYING}`。

    与 `parse_signal_state_key` 同源判定（租户段为空才算历史形态），不另写一套正则。
    这种形态只用于迁移与清理，**写侧绝不能落库**（A16，2026-10-27）：
    另一进程周期开始时 load 到的内存副本可能仍含旧键，整份 state 回写会把它
    复活 → 迁移 `pop` 白做、规范键与旧键两份并存。
    """
    tenant_id, underlying = parse_signal_state_key(key, prefix)
    return underlying is not None and not tenant_id


def without_legacy_signal_state_keys(
    state: dict, prefix: str = "volatility_long_gamma:",
) -> dict:
    """返回剔除历史无租户信号键的**副本** —— 落盘前必须过这一层（A16）。

    返回副本而不是原地删：内存里的旧键还要留给 `volatility_signal_state()`
    做「只搬不复制」迁移，删掉就丢失了一次计时。
    """
    return {
        key: value for key, value in (state or {}).items()
        if not is_legacy_signal_state_key(key, prefix)
    }


def volatility_state_keys(
    code: str, tenant_id: str = "", underlying: str = "", prefix: str = "volatility_long_gamma:",
) -> set[str]:
    """同一份状态键的**全部形态** —— 带租户（新写入）与不带租户（历史）。

    铁律：**写侧用哪种形态，清理侧就必须认得哪种。**
    10-23 P0-9 实测（Architect 定位，每一轮都在发生）：
    `order_push` 写 `volatility_long_gamma:{tenant}:{code}`（带租户，跨租户隔离所需），
    而 `run_volatility_strategy` 的 `active_keys` 与 `prune_campaign_states`
    只认 `volatility_long_gamma:{code}`（不带租户）→ 刚写进去的键
    **同一个周期就被当 stale 删掉**，P0-1 的「成交即登记」被完全抵消。

    所以这里**一次性产出两种形态**，写侧、清理侧、保护侧都从它取 ——
    不再各自拼字符串（各自拼就是下一次不对称的起点）。
    """
    code = str(code or "").strip()
    tenant_id = str(tenant_id or "").strip()
    underlying = str(underlying or "").strip().upper()
    keys = {f"{prefix}{code}"}
    if tenant_id:
        keys.add(f"{prefix}{tenant_id}:{code}")
    if underlying:
        keys.add(f"{prefix}signal:{underlying}")
        if tenant_id:
            # P0-9 既有形态（campaign 键命名空间）与 2026-10-27 规范形态都要认，
            # 否则加了租户段之后旧形态的保护集合会漏掉。
            keys.add(f"{prefix}{tenant_id}:signal:{underlying}")
            keys.add(volatility_signal_state_key(underlying, tenant_id, prefix=prefix))
    return keys


def active_volatility_state_keys(
    campaigns: Iterable[dict],
    tenant_id: str = "",
    protected: Iterable[str] | None = None,
    prefix: str = "volatility_long_gamma:",
) -> set[str]:
    """构建「本轮活跃」的 campaign 状态键集合 —— **清理侧与测试共用同一实现**。

    存在的意义：P0-9 的守卫如果自己拼 `active_keys`，就**绕过了生产代码**，
    把生产代码改错也照样绿（10-23 变异测试实测：去掉带租户形态，守卫仍绿）。
    清理侧与守卫都调这里，生产一改错，守卫立刻转红。
    """
    keys = set(protected or ())
    for item in campaigns:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        keys |= volatility_state_keys(
            code, tenant_id, str(item.get("underlying") or ""), prefix=prefix,
        )
    return keys


def protected_volatility_state_keys(
    store,
    positions: pd.DataFrame | None,
    orders: pd.DataFrame | None,
    broker,
    prefix: str = "volatility_long_gamma:",
    tenant_id: str = "",
) -> set[str]:
    """State keys that must not be pruned while broker risk still exists.

    `tenant_id` 可显式传入；缺省时走**共享入口** :func:`tenant_of_broker`。
    **两种形态都产出**（见 `volatility_state_keys`）—— 否则带租户的键得不到保护，
    「有券商持仓就不许剪」这条护栏对它们失效（P0-9 的第二处漏）。

    **不得改回 ``getattr(broker, "tenant_id", "")``**：生产 ``FutuPaperBroker``
    从未被赋值该属性 → 恒空 → 本函数只产无租户形态，带租户键全部失去保护。
    """
    if not tenant_id:
        tenant_id = tenant_of_broker(broker, strict=False)[0] or ""
    protected: set[str] = set()
    if positions is not None and not positions.empty and "code" in positions.columns:
        for _, row in positions.iterrows():
            code = str(row.get("code") or "").upper()
            if parse_option_code(code) and abs(_number(row.get("qty", row.get("quantity")))) > 1e-9:
                protected |= volatility_state_keys(code, tenant_id, prefix=prefix)
    if orders is not None and not orders.empty and "code" in orders.columns:
        pending = _pending_counts(orders, broker)
        for code, count in pending.items():
            if count > 0:
                protected |= volatility_state_keys(str(code).upper(), tenant_id, prefix=prefix)
    strategy_prefix = prefix.rstrip(":")
    for item in store.shadow_open_positions():
        if str(item.get("strategy_id")) == strategy_prefix:
            code = str(item.get("symbol") or "").upper()
            if parse_option_code(code):
                protected |= volatility_state_keys(code, tenant_id, prefix=prefix)
    return protected


def manage_volatility_orphans(
    *,
    broker,
    executor,
    campaigns: list[dict],
    positions: pd.DataFrame,
    orders: pd.DataFrame,
    now: datetime | None = None,
) -> list[dict]:
    """Audit and optionally submit reduce-only exits for unmanaged strategy positions."""
    now = now or datetime.now(timezone.utc)
    store = executor.store
    orphans = detect_volatility_orphan_positions(
        store, positions, orders, broker, campaigns,
    )
    if not orphans:
        store.recover_incident("volatility-orphan-broker-positions")
        return []
    payload = [item.to_dict() for item in orphans]
    message = f"发现 {len(orphans)} 个波动率期权未接管富途持仓，已进入安全处置流程"
    store.record_incident("volatility-orphan-broker-positions", "ERROR", message)
    store.audit("VOLATILITY_ORPHAN_POSITIONS_DETECTED", {"positions": payload}, "volatility_long_gamma", actor="service:radar")
    policy = store.get_setting(
        "volatility_orphan_position_policy",
        os.getenv("VOLATILITY_ORPHAN_POSITION_POLICY", "ADOPT"),
    ).upper()
    if policy not in {"ADOPT", "CLOSE", "BLOCK"}:
        policy = "ADOPT"
    results: list[dict] = []
    quote_map: dict[str, dict] = {}
    sellable_codes = [item.code for item in orphans if item.status == "SELLABLE_ORPHAN"]
    if sellable_codes:
        try:
            quotes = broker.snapshots(sellable_codes)
            if quotes is not None and not quotes.empty and "code" in quotes.columns:
                quote_map = {
                    str(row.get("code") or "").upper(): row.to_dict()
                    for _, row in quotes.iterrows()
                }
        except Exception as exc:
            store.audit(
                "VOLATILITY_ORPHAN_QUOTE_FAILED",
                {"error": str(exc), "codes": sellable_codes},
                "volatility_long_gamma",
             actor="service:radar")
    for item in orphans:
        base = item.to_dict()
        if policy == "ADOPT":
            # 跨租户隔离：campaign 带 tenant_id，避免同 code 不同租户互相连坐。
            # 走共享入口 tenant_of_broker —— 裸 getattr(broker, "tenant_id") 恒空，
            # 会让接管出的 campaign 退化成全局形态、两租户同 code 互相顶掉（P0-9 的第三处漏）。
            tenant_id = tenant_of_broker(broker, strict=False)[0] or ""
            adopted = adopt_volatility_orphan_position(store, item, campaigns, tenant_id)
            reason = "未接管仓位已自动接管：后续纳入波动率策略止盈、止损、盈利保护和方向退出"
            store.decision("volatility_long_gamma", item.code, "HOLD", reason, {
                **base, "adopted_campaign": adopted,
            }, tenant_id=tenant_id)
            results.append({
                **base,
                "action": "ADOPTED",
                "result": reason,
                "campaign": adopted,
            })
            continue
        if policy == "BLOCK":
            reason = "发现未接管仓位；当前策略配置为只提示不处置，等待人工处理"
            store.decision("volatility_long_gamma", item.code, "BLOCKED", reason, base, tenant_id=tenant_id)
            results.append({**base, "action": "BLOCKED", "result": reason})
            continue
        if item.pending_order_count:
            reason = "未接管仓位已有待成交退出订单，等待成交回报"
            store.decision("volatility_long_gamma", item.code, "HOLD", reason, base, tenant_id=tenant_id)
            results.append({**base, "action": "WAIT_PENDING", "result": reason})
            continue
        if item.can_sell_quantity <= 0:
            reason = "发现未接管仓位，但当前可卖数量为 0，等待券商解锁"
            store.decision("volatility_long_gamma", item.code, "BLOCKED", reason, base, tenant_id=tenant_id)
            results.append({**base, "action": "LOCKED", "result": reason})
            continue
        quote = quote_map.get(item.code, {})
        bid = _number(quote.get("bid_price"))
        reference = bid if bid > 0 else item.nominal_price
        if reference <= 0:
            reason = "发现未接管仓位，但盘口和现价均不可用，无法安全提交退出单"
            store.decision("volatility_long_gamma", item.code, "BLOCKED", reason, base, tenant_id=tenant_id)
            results.append({**base, "action": "NO_PRICE", "result": reason})
            continue
        price = max(0.01, round(reference * (0.98 if bid > 0 else 0.90), 2))
        result = executor.submit_limit(
            "volatility_long_gamma",
            f"{now.date().isoformat()}:ORPHAN_EXIT:{item.code}",
            item.code,
            "SELL",
            min(item.quantity, item.can_sell_quantity),
            price,
            {
                "multiplier": 100.0,
                "underlying": item.underlying,
                "urgent": True,
                "reduce_only": True,
                "orphan_recovery": True,
                "reason": item.reason,
                "reference_price": reference,
                "orphan_position": base,
            },
        )
        reason = f"未接管仓位自动提交平仓：{result.message}"
        store.decision("volatility_long_gamma", item.code, "SELL", reason, {
            **base, "exit_price": price, "execution_status": result.status,
            "broker_order_id": result.broker_order_id,
        }, tenant_id=tenant_id)
        results.append({
            **base,
            "action": "SELL_SUBMITTED",
            "exit_price": price,
            "result": result.message,
            "broker_order_id": result.broker_order_id,
            "execution_status": result.status,
        })
    store.audit("VOLATILITY_ORPHAN_POSITIONS_HANDLED", {"results": results}, "volatility_long_gamma", actor="service:radar")
    return results


def adopt_volatility_orphan_position(
    store,
    item: VolatilityOrphanPosition,
    campaigns: list[dict],
    tenant_id: str = "",
) -> dict:
    """Create a defensive exit-only campaign/state for an unmanaged broker position.

    `tenant_id` 写入 campaign，供跨租户隔离判定（同 code 不同租户互不干扰）。
    """
    from .config import upsert_campaign

    contract = parse_option_code(item.code)
    template = next(
        (campaign for campaign in campaigns if str(campaign.get("underlying", "")).upper() == item.underlying.upper()),
        {},
    )
    latest_buy = _latest_strategy_buy(store, "volatility_long_gamma", item.code) or {}
    opened_at = str(latest_buy.get("created_at") or datetime.now(timezone.utc).isoformat())
    cost = item.cost_price or _number(latest_buy.get("average_fill_price")) or item.nominal_price
    peak = max(item.nominal_price, cost, _number(template.get("peak")))
    expiry = contract.expiration.isoformat() if contract else str(template.get("strike_time", ""))
    adopted = {
        **template,
        "campaign_id": f"{tenant_id}|orphan:{item.code}" if tenant_id else f"orphan:{item.code}",
        "code": item.code,
        "underlying": item.underlying,
        "option_type": contract.option_type if contract else template.get("option_type", "CALL"),
        "strike_time": expiry,
        "event_date": datetime.now(timezone.utc).date().isoformat(),
        "event_session": template.get("event_session", "盘中"),
        "trigger_mode": "VOLATILITY",
        "structure": "LONG_OPTION",
        "legs": [{"code": item.code, "side": "BUY", "ratio": 1, "role": "PRIMARY"}],
        "enabled": True,
        "exit_only": True,
        "orphan_adopted": True,
        "orphan_adopted_at": datetime.now(timezone.utc).isoformat(),
        "orphan_adopted_reason": item.reason,
        "contracts_per_stage": int(template.get("contracts_per_stage", 1) or 1),
        "max_stages": int(template.get("max_stages", 1) or 1),
        "max_premium_per_stage": float(template.get("max_premium_per_stage", 3000) or 3000),
        "profit_target": float(template.get("profit_target", .50) or .50),
        "stop_loss": float(template.get("stop_loss", .25) or .25),
        "trailing_stop": float(template.get("trailing_stop", .25) or .25),
        "entry_hours": int(template.get("entry_hours", 6) or 6),
        "max_hold_hours": float(template.get("max_hold_hours", 24) or 24),
        # F（关键）：接管产生的 campaign **必须**带显式止损限额。
        # 模板没有就写默认值 —— 否则 P0-3 的 fail-closed 会被这条写入路径绕过
        # （缺省即拒绝开仓，等于这条路径持续产出「开不了仓」的 campaign）。
        "max_position_loss_ratio": float(template.get("max_position_loss_ratio", 0.25) or 0.25),
        "max_position_loss_usd": float(template.get("max_position_loss_usd", 300.0) or 300.0),
        "tenant_id": str(tenant_id or "").strip(),
    }
    # #1 键级分区：写入**租户作用域的键**，与读侧（config.load_volatility_campaigns(tenant)）
    # 同一把钥匙。键的拼接收敛在 tenant 层，这里不手拼；解析不出租户时 lenient 会
    # 回落全局键并置 `_key_fell_back`，由调用方决定是否记事件 —— 不静默假装隔离成立。
    _campaigns_key, _key_fell_back = campaigns_config_key_lenient(tenant_id)
    upsert_campaign(_campaigns_key, adopted, store)
    # #1 键级分区（读侧兼容）：状态键与写侧同形。
    # `order_push` 写 `volatility_long_gamma:{tenant}:{code}`；这里若仍写无租户键，
    # 两条路径各自看到一份状态、互相覆盖 → peak 错 → 止盈止损基准错。
    # 读侧兼容旧的无租户键（生产存量 8 条），读到后就地收敛成新键；写侧只写新格式。
    _state_tenant = str(tenant_id or "").strip()
    # 写形态走**共享助手**（#1）：四个读写点必须用同一个拼法，
    # 否则同一份状态会裂成两个键、读侧各看各的（peak 错 → 止盈止损基准错）。
    _state_key = volatility_write_state_key(item.code)                  # 迁移前形态（无租户）
    state_key = volatility_write_state_key(item.code, _state_tenant)   # 写形态（带租户）
    states = store.campaign_states()
    if state_key != _state_key and state_key not in states and _state_key in states:
        # 读侧回落：迁移前的存量键 → 就地收敛成带租户形态（只搬不复制）
        state = dict(states.get(_state_key) or {})
    else:
        state = dict(states.get(state_key) or {})
    state.update({
        "previous_position_quantity": int(item.quantity),
        "position_opened_at": opened_at,
        "peak": peak,
        "entry_iv_actual": float(state.get("entry_iv_actual", template.get("entry_iv", 0)) or 0),
        "entry_gamma_theta_efficiency_actual": float(
            state.get(
                "entry_gamma_theta_efficiency_actual",
                template.get("entry_gamma_theta_efficiency", 0),
            ) or 0
        ),
        "signal_active": bool(state.get("signal_active", True)),
        "regime_was_active": bool(state.get("regime_was_active", True)),
        "orphan_adopted": True,
        "orphan_adopted_at": adopted["orphan_adopted_at"],
        "orphan_cost_price": item.cost_price,
        "orphan_nominal_price": item.nominal_price,
    })
    store.save_campaign_states({state_key: state})
    store.audit("VOLATILITY_ORPHAN_POSITION_ADOPTED", {
        "code": item.code,
        "underlying": item.underlying,
        "quantity": item.quantity,
        "cost_price": item.cost_price,
        "nominal_price": item.nominal_price,
        "pnl_value": item.pnl_value,
        "pnl_ratio": item.pnl_ratio,
        "campaign": adopted,
    }, "volatility_long_gamma", actor="service:radar")
    return adopted


def orphan_rows_for_display(
    store,
    positions: pd.DataFrame,
    orders: pd.DataFrame,
    broker,
    campaigns: list[dict],
) -> list[dict]:
    return [item.to_dict() for item in detect_volatility_orphan_positions(
        store, positions, orders, broker, campaigns,
    )]
