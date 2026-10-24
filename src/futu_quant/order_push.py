from __future__ import annotations

import os
import json
from datetime import datetime, timezone

from futu import RET_OK, TradeDealHandlerBase, TradeOrderHandlerBase

from .fee_model import estimate_fees
from .tenant import campaigns_config_key_lenient
from .volatility_orphans import volatility_write_state_key


class OrderPushHandler(TradeOrderHandlerBase):
    def __init__(self, store, broker=None):
        super().__init__()
        self.store = store
        self.broker = broker

    def _tenant_id(self) -> str:
        """当前推送通道归属的租户（用于隔离 state 与 campaign 配置）。

        **必须走共享入口** `tenant_of_broker(..., strict=True)`：
        `broker.tenant_id` 在生产里**从未被赋值**（`FutuPaperBroker` 只带
        `account_id`），所以 `getattr(broker, "tenant_id", "")` 恒为空 ——
        这正是 P0-1 跨租户隔离长期是 no-op 的根因（租户为空 → `owned` 恒为 True
        → 实盘成交被模拟 campaign 判成「已有归属」而不登记）。

        解析失败时**不抛**（抛了会中断整个推送 handler），而是返回空串 ——
        由 `register_fill_to_campaign` 入口的 fail-closed 守卫接住：
        记 `FILL_REGISTRATION_TENANT_MISSING`（ERROR）并跳过，**绝不写全局键**。
        这样既 fail-closed，又不让一次异常打死整条推送通道。
        """
        from .tenant import tenant_of_broker

        try:
            tenant_id, _ = tenant_of_broker(self.broker, strict=True)
        except Exception:
            return ""
        return str(tenant_id or "")

    def _tenant_label(self) -> str:
        tenant = self._tenant_id()
        return f"租户 {tenant}" if tenant else "租户未知"

    def on_recv_rsp(self, rsp_pb):
        code, data = super().on_recv_rsp(rsp_pb)
        if code != RET_OK:
            self.store.operational_event("order_push", "ORDER_PUSH_ERROR", "ERROR", {"error": str(data)})
            return code, data
        for _, row in data.iterrows():
            # 不按环境过滤：推送通道按租户/账户订阅，intent 反查只可能命中本租户的订单。
            # 真金侧同样需要订单推送来驱动账本与持仓登记。
            status = str(row.get("order_status", "")).upper().split(".")[-1].replace("ORDERSTATUS_", "")
            filled = float(row.get("dealt_qty", 0) or 0)
            average = float(row.get("dealt_avg_price", 0) or 0)
            fees = estimate_fees(str(row.get("code", "")), str(row.get("trd_side", "BUY")), filled, average) if filled else 0
            self.store.update_by_broker_order(
                str(row["order_id"]), status, filled, average, fees, source="BROKER_PUSH",
            )
            self.store.operational_event("order_push", "ORDER_UPDATE", "HEALTHY", row.to_dict())
        self.store.heartbeat(
            "order_push", "HEALTHY",
            # E：文案与实际不符 —— 现在 REAL 也走这条路（10-22 起去掉了 SIMULATE 过滤）
            f"订单推送已接收（{self._tenant_label()}）", os.getpid(),
        )
        return code, data


class DealPushHandler(TradeDealHandlerBase):
    def __init__(self, store, broker=None):
        super().__init__()
        self.store = store
        # 券商句柄用于「成交即登记」时**确认该合约真的在持仓里**。
        # 10-22 事故：这里此前传 None，导致券商未确认的成交也会建 exit_only campaign，
        # 凭空产生幽灵持仓（无 intent、无 fill、无券商持仓，每轮被静默跳过）。
        self.broker = broker

    def _tenant_id(self) -> str:
        """当前推送通道归属的租户（成交登记必须按租户隔离）。

        走共享入口 `tenant_of_broker(..., strict=True)` —— 同 `OrderPushHandler`。
        解析失败返回空串（不抛，避免打死推送通道），由登记函数的
        fail-closed 守卫接住并记 ERROR。
        """
        from .tenant import tenant_of_broker

        try:
            tenant_id, _ = tenant_of_broker(self.broker, strict=True)
        except Exception:
            return ""
        return str(tenant_id or "")

    def on_recv_rsp(self, rsp_pb):
        code, data = super().on_recv_rsp(rsp_pb)
        if code == RET_OK:
            for _, row in data.iterrows():
                # 不按环境过滤（历史缺陷）：真金成交此前被 SIMULATE 过滤掉，record_fill 从不执行，
                # 真金开仓只能等轮询兜底 → 存在「无保护窗口」。推送本就按租户订阅，
                # intent 反查只会命中本租户订单，无需再用环境字段兜底过滤。
                payload = row.to_dict()
                order_id = str(row.get("order_id", ""))
                intent = self.store.intent_by_broker_order(order_id)
                if intent:
                    qty = float(row.get("dealt_qty", row.get("qty", 0)) or 0)
                    price = float(row.get("dealt_avg_price", row.get("price", 0)) or 0)
                    deal_id = row.get("deal_id", row.get("dealId"))
                    if qty > 0 and price > 0:
                        self.store.record_fill(
                            intent["id"], qty, price,
                            float(row.get("commission", 0) or 0),
                            source="BROKER_DEAL", broker_deal_id=str(deal_id) if deal_id else None,
                            reference_price=json.loads(intent["metadata_json"] or "{}").get("reference_price"),
                            multiplier=float(json.loads(intent["metadata_json"] or "{}").get("multiplier", 1)),
                        )
                        # 成交即登记归属：有 campaign → 标记；无归属 → 当场 ADOPT（不等下一周期）
                        # 传 tenant_id —— 跨租户隔离（产品红线，10-23）
                        register_fill_to_campaign(
                            self.store, self.broker, intent, qty, price, self._tenant_id(),
                        )
                self.store.operational_event("order_push", "DEAL_UPDATE", "HEALTHY", payload)
        return code, data


def register_fill_to_campaign(
    store, broker, intent: dict, qty: float, price: float, tenant_id: str = "",
) -> dict | None:
    """成交即登记归属：有 campaign → 标记；无归属 → **当场**建 exit-only campaign。

    背景（10-22 实盘事故，系统性缺陷而非个例）：真金成交此前被 SIMULATE 过滤，
    既不进账本也不进 campaign；即便入账，也要等下一个策略周期由孤儿检测器补登记。
    这两段延迟叠加成「无保护窗口」—— 止损评估只遍历 campaign 列表，窗口内仓位
    完全不可见。META 看跌就是这样从 $300 止损线一路跑到 $366 才被动平仓。

    本函数把登记时机提前到**成交推送内**（幂等）：
    - 该 code 已有活跃 campaign → 在 campaign state 上标记已登记（幂等，重复调用无副作用）；
    - 无归属 → 立即 ADOPT 成 `exit_only` campaign，**不等下一个周期**。

    **跨租户隔离（产品红线，10-23）**：`tenant_id` 必须传入。
    - state key 带租户维度（`volatility_long_gamma:{tenant}:{symbol}`）——
      否则实盘与模拟同 code 会共用 state；
    - 「已有归属」的判定只在本租户的 campaign 里找 —— 否则模拟租户的同 code
      campaign 会让实盘成交被误判「已有归属」而不登记（反之亦然）；
    - 新建的 campaign 带 `tenant_id`，推送线程写配置限定本租户。

    返回登记结果；任何异常都不得阻断成交推送主流程（回落为「本周期由孤儿检测器兜底」），
    但**必须留告警** —— 静默的失败会让整类故障无声（10-22 的教训）。
    """
    from .config import load_volatility_campaigns, upsert_campaign
    from .option_lifecycle import parse_option_code
    from .volatility_orphans import adopt_volatility_orphan_position, VolatilityOrphanPosition

    symbol = str((intent or {}).get("symbol") or "").upper()
    tenant_id = str(tenant_id or "").strip()
    if not symbol or parse_option_code(symbol) is None:
        return None
    # fail-closed：租户为空**抛异常**，绝不静默写无前缀全局键（跨租户连坐）。
    # 正常路径由 handler 的 `_tenant_id()`（strict=True）保证非空；
    # 这一道是防「调用方显式传空」——那正是被治的静默回落。
    if not tenant_id:
        store.operational_event(
            "order_push", "FILL_REGISTRATION_TENANT_MISSING", "ERROR",
            {"code": symbol, "reason": "成交登记缺少租户上下文 —— 拒绝写全局键（跨租户隔离）"},
        )
        return {"action": "SKIPPED_TENANT_MISSING", "code": symbol}
    try:
        # #1 键级分区（D 批）：读也走**租户键** —— 否则读全局键会看到别家租户的 campaign，
        # 「已有归属」判定就会跨租户误判（模拟的同 code campaign 让实盘成交被认为"已有归属"）。
        _campaigns_key, _key_fell_back = campaigns_config_key_lenient(tenant_id)
        if _key_fell_back:
            store.operational_event(
                "order_push", "CAMPAIGNS_KEY_TENANT_UNRESOLVED", "ERROR",
                {"code": symbol, "tenant_id": tenant_id,
                 "message": "无法把 campaign 清单解析到租户键 —— 回落无前缀旧键（未隔离，已留痕）"},
            )
        campaigns = load_volatility_campaigns(tenant_id)
    except Exception as exc:
        # C：此前是裸 `return None`（无声）。读不到配置是最该报警的一类失败 ——
        # 它意味着成交既登记不进 campaign，也没人知道。
        store.operational_event(
            "order_push", "FILL_REGISTRATION_CONFIG_UNREADABLE", "ERROR",
            {"code": symbol, "tenant_id": tenant_id, "error": str(exc)},
        )
        return None

    def _owned_by_tenant(item: dict) -> bool:
        """campaign 归属本租户：显式带 tenant_id 的必须匹配；未带的视为历史数据，
        仅在**只有一个候选租户**时不误判 —— 这里采取保守策略：只认显式匹配，
        未标注 tenant_id 的历史 campaign 不参与跨租户判定（避免误判「已有归属」）。"""
        item_tenant = str(item.get("tenant_id") or "").strip()
        if item_tenant:
            return item_tenant == tenant_id
        return False

    owned = any(
        str(item.get("code") or "").upper() == symbol and _owned_by_tenant(item)
        for item in campaigns
    )
    if owned:
        # 已有归属：标记该 campaign 已被成交登记（幂等），供界面与审计观察时效
        _mark_fill_registered(store, symbol, qty=qty, price=price, tenant_id=tenant_id)
        return {"action": "MARKED", "code": symbol, "tenant_id": tenant_id}

    # 无归属 → 当场 ADOPT。构造最小 orphan 条目；缺 broker 时仍建 exit_only campaign，
    # 保证「先有归属、再由退出管理补齐成本价」，而不是等下一周期。
    try:
        if broker is not None:
            positions = broker.positions()
            row = positions[positions["code"].astype(str).str.upper() == symbol]
            if not row.empty:
                parsed = parse_option_code(symbol)
                underlying = str((intent or {}).get("underlying") or "")
                if not underlying and parsed is not None:
                    underlying = f"{symbol.split('.')[0]}.{parsed.underlying}"
                item = VolatilityOrphanPosition(
                    code=symbol,
                    underlying=underlying or symbol,
                    quantity=int(float(row.iloc[0].get("qty") or 0)),
                    can_sell_quantity=int(float(row.iloc[0].get("can_sell_qty") or 0)),
                    cost_price=float(row.iloc[0].get("cost_price") or price),
                    nominal_price=float(row.iloc[0].get("nominal_price") or price),
                    market_value=float(row.iloc[0].get("market_value") or 0),
                    pnl_value=float(row.iloc[0].get("pl_val") or 0),
                    pnl_ratio=float(row.iloc[0].get("pl_ratio") or 0),
                    strategy_net_quantity=float(qty),
                    pending_order_count=0,
                    status="SELLABLE_ORPHAN",
                    reason="成交时无 campaign 归属，当场采用",
                )
                adopted = adopt_volatility_orphan_position(store, item, campaigns, tenant_id)
                # P0-4 裁定：ADOPT 路径**不**写 `fill_registered_*`（保持既有语义：
                # 该字段由 MARKED 路径登记）。这三个字段读侧无消费、属历史留痕，
                # 严禁新逻辑依赖 —— 见 tests/test_fill_registration_is_audit_only.py。
                return {"action": "ADOPTED", "code": symbol, "campaign": adopted}
            # 10-22 幽灵 campaign 根因：券商快照**没有**该合约时，此前仍会建一条
            # exit_only campaign —— 于是凭空多出一个合约的「持仓」，
            # 而它既无 intent、无 fill、无券商持仓，只能每轮被静默跳过。
            # 铁律：**券商未确认的成交不得建 campaign**；交由下一周期孤儿检测器
            # （它要求「券商持仓 qty>0」且「策略净持仓>0」）兜底。
            store.operational_event(
                "volatility_long_gamma", "FILL_ADOPTION_SKIPPED_UNVERIFIED", "WARNING",
                {
                    "symbol": symbol, "qty": float(qty), "price": float(price),
                    "reason": "成交推送的合约未出现在券商持仓快照中，不建立 campaign（避免幽灵持仓）",
                },
            )
            return {"action": "SKIPPED_UNVERIFIED", "code": symbol}
        # 确实拿不到券商（例如探测/测试环境）：只有在 intent 本身**有券商订单号**时
        # 才建最小 exit_only campaign。没有券商订单号的 intent 不可能是真实成交，
        # 为它建 campaign 就是制造幽灵持仓（10-22 实测过一条）。
        if not (intent or {}).get("broker_order_id"):
            store.operational_event(
                "volatility_long_gamma", "FILL_ADOPTION_SKIPPED_NO_BROKER_ORDER", "WARNING",
                {
                    "symbol": symbol, "qty": float(qty), "price": float(price),
                    "intent_id": (intent or {}).get("id"),
                    "reason": "intent 无券商订单号，不是真实成交，不建立 campaign",
                },
            )
            return {"action": "SKIPPED_NO_BROKER_ORDER", "code": symbol}
        adopted = {
            "campaign_id": f"{tenant_id}|orphan:{symbol}" if tenant_id else f"orphan:{symbol}",
            "code": symbol,
            "underlying": symbol.rsplit(".", 1)[0] if "." in symbol else symbol,
            "option_type": (parse_option_code(symbol).option_type if parse_option_code(symbol) else "CALL"),
            "strike_time": (parse_option_code(symbol).expiration.isoformat() if parse_option_code(symbol) else ""),
            "event_date": datetime.now(timezone.utc).date().isoformat(),
            "trigger_mode": "VOLATILITY",
            "structure": "LONG_OPTION",
            "legs": [{"code": symbol, "side": "BUY", "ratio": 1, "role": "PRIMARY"}],
            "enabled": True,
            "exit_only": True,
            "orphan_adopted": True,
            "orphan_adopted_at": datetime.now(timezone.utc).isoformat(),
            "orphan_adopted_reason": "成交时无 campaign 归属，当场采用（推送路径，未经券商确认）",
            "orphan_adoption_verified": False,
            # F（关键）：推送路径新建的 campaign **必须**带显式止损限额。
            # 否则 P0-3 的 fail-closed 会被自己的写入路径绕过 —— 配置补齐只覆盖
            # 存量 campaign 是治标不治本，这条路径会持续产出缺键 campaign。
            "max_position_loss_ratio": 0.25,
            "max_position_loss_usd": 300.0,
            "tenant_id": tenant_id,
            "contracts_per_stage": 1,
            "max_stages": 1,
            "stop_loss": 0.35,
            "trailing_stop": 0.25,
            "profit_target": 2.0,
            "max_hold_hours": 24,
        }
        upsert_campaign(_campaigns_key, adopted, store)
        store.audit("VOLATILITY_FILL_ADOPTED_IMMEDIATE", {
            "code": symbol, "qty": float(qty), "price": float(price),
            "intent_id": (intent or {}).get("id"), "tenant_id": tenant_id,
            "max_position_loss_usd": adopted["max_position_loss_usd"],
            "max_position_loss_ratio": adopted["max_position_loss_ratio"],
        }, "volatility_long_gamma", actor="service:order_push")
        return {"action": "ADOPTED", "code": symbol, "campaign": adopted}
    except Exception as exc:
        store.operational_event(
            "order_push", "FILL_REGISTRATION_FAILED", "WARNING",
            {"code": symbol, "error": str(exc)},
        )
        return None


def _mark_fill_registered(store, symbol: str, *, qty, price, tenant_id: str) -> None:
    """把「本笔成交已登记」写进 campaign 状态（owned 与 ADOPT 两条路径共用）。

    P0-4 复核结论：这三个字段**读侧当前无消费**，属审计留痕；真正的保护是成交账本
    `store.fills()`。但只在 owned 分支写会让它变成半状态字段 —— 两条路径都写，
    语义才一致；写入失败必须可见（不能裸 pass）。
    """
    try:
        # 租户为空时**抛异常**，绝不退回无前缀全局键形态 ——
        # 那只是「把一处静默回落换成另一处静默回落」（Reviewer 裁定，Product 红线）。
        state_key = volatility_write_state_key(symbol, tenant_id)
        states = store.campaign_states()
        state = dict(states.get(state_key) or {})
        if not state.get("fill_registered_at"):
            state["fill_registered_at"] = datetime.now(timezone.utc).isoformat()
            state["registered_fill_qty"] = float(qty)
            state["registered_fill_price"] = float(price)
            store.save_campaign_states({state_key: state})
    except Exception as exc:
        store.operational_event(
            "order_push", "FILL_REGISTRATION_MARK_FAILED", "WARNING",
            {"code": symbol, "tenant_id": tenant_id, "error": str(exc)},
        )


def open_order_push(broker, store):
    context = broker.new_trade_context()
    context.set_handler(OrderPushHandler(store, broker))
    context.set_handler(DealPushHandler(store, broker))
    store.heartbeat("order_push", "HEALTHY", "推送通道已连接，轮询作为兜底", os.getpid())
    store.set_setting("order_push_connected", "true")
    store.operational_event("order_push", "CONNECTED", "HEALTHY")
    return context
