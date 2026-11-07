"""
接近市价的下单-撤单实机探针（Near-Market Submit & Cancel Probe）。

背景：`probe_order_submit_cancel.py` 的 $0.01 极低价会被富途网关以
「订单价格过于保守」直接拒单（券商侧拒绝，挂都不挂），验证不到撤单路径。
本脚本把限价挂在买一价下方一个小百分比（默认 2%）——挂单能被券商接受、
但在秒级窗口内几乎不可能成交——提交后立即撤单，验证完整闭环。

安全铁律：
- 只允许 BUY，严禁 SELL（防止意外贱卖持仓）；
- 数量固定 1 股/张；
- 限价必须严格低于当前买一价（非市场化，不追价）；
- 实盘租户必须显式 --force-real。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

if sys.platform == "win32":
    _local_appdata = Path(__file__).resolve().parents[1] / "data" / "appdata"
    _local_appdata.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(_local_appdata)

from futu_quant.broker import FutuPaperBroker, close_all_connections
from futu_quant.config import Settings
from futu_quant.execution import ExecutionManager
from futu_quant.store import TradingStore
from futu_quant.tenant import resolve_tenant


def _print_safe(*args, **kwargs) -> None:
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        fallback = " ".join(str(arg) for arg in args)
        print(fallback.encode("ascii", "replace").decode("ascii"), **kwargs)


def _parse_option(code: str):
    from futu_quant.option_lifecycle import parse_option_code
    return parse_option_code(code)


def _reference_price(broker, symbol: str) -> tuple[float, float, float]:
    """返回 (bid, ask, 参考价)。期权与正股统一走 snapshots。"""
    quote = broker.snapshots([symbol])
    row = quote[quote["code"] == symbol].iloc[0]
    bid = float(row.get("bid_price", 0) or 0)
    ask = float(row.get("ask_price", 0) or 0)
    last = float(row.get("last_price", 0) or 0)
    ref = bid if bid > 0 else (ask if ask > 0 else last)
    if ref <= 0:
        raise RuntimeError(f"无法获取 {symbol} 有效报价: bid={bid} ask={ask} last={last}")
    return bid, ask, ref


def run_probe(
    tenant_id: str = "futu_real_master",
    symbol: str = "US.AAPL",
    below_bid_pct: float = 0.02,
    force_real: bool = False,
) -> int:
    settings = Settings.load()
    tenant = resolve_tenant(tenant_id, strict=True)
    if not tenant:
        _print_safe(f"[✗] 无法解析租户 '{tenant_id}'")
        return 1

    is_real = tenant.trading_env == "REAL"
    if is_real and not force_real:
        _print_safe("[!] 目标租户为 REAL 实盘，必须显式 --force-real")
        return 2

    from futu_quant.option_lifecycle import parse_option_code
    is_option = parse_option_code(symbol) is not None
    # 期权乘数 100：1 张 ≈ 权利金×100 美元的风险敞口，实盘期权探针必须显式确认
    if is_option and is_real and below_bid_pct < 0.02:
        _print_safe("[!] 实盘期权探针要求 below_bid_pct >= 0.02（防止贴价成交）")
        return 2

    store = TradingStore()
    if is_real:
        from futu_quant.real_trading import session_status, enable_session, confirmation_code
        active, reason, _ = session_status(store, settings, account_id=tenant.account_id)
        if not active:
            ok, msg = enable_session(store, settings, confirmation_code(settings),
                                     minutes=10, actor="cli:probe_near")
            _print_safe(f"[*] 实盘确认会话自动激活(10分钟): {msg}")

    broker = FutuPaperBroker(
        settings,
        trading_env_override=tenant.trading_env,
        account_id_override=tenant.account_id,
    )
    executor = ExecutionManager(broker, store, tenant=tenant)

    bid, ask, ref = _reference_price(broker, symbol)
    price = round(ref * (1.0 - below_bid_pct), 2)
    # 限价必须严格低于买一（非市场化）；受最小价格档位保护，再向下取一分
    if bid > 0 and price >= bid:
        price = round(bid - 0.01, 2)
    if price <= 0:
        _print_safe("[✗] 计算出的限价非法")
        return 2

    env_tag = "实盘真金" if is_real else "模拟沙盒"
    _print_safe("=" * 60)
    _print_safe(f"  接近市价下单撤单探针 [{env_tag}] 租户={tenant.tenant_id}")
    _print_safe(f"  标的: {symbol} | 买一: {bid} | 卖一: {ask}")
    _print_safe(f"  限价: ${price} (买一下方 {below_bid_pct:.1%}) | 数量: 1")
    _print_safe("=" * 60)

    strategy_id = (
        "volatility_long_gamma" if is_option else "core_satellite"
    ) if is_real else "probe_test"
    intent_key = f"probe_near_{int(time.time() * 1000)}:{symbol}"
    metadata = {
        "smart_execution": False,
        "probe_test": True,
        "operator": "probe_near_market",
        "reference_price": price,
    }

    t0 = time.perf_counter()
    _print_safe(f"[*] [1/3] 提交限价买单 ${price} ...")
    res = executor.submit_limit(
        strategy_id=strategy_id,
        intent_key=intent_key,
        symbol=symbol,
        side="BUY",
        quantity=1,
        price=price,
        metadata=metadata,
        execution_mode_override="REAL" if is_real else "PAPER",
    )
    t_submit = time.perf_counter() - t0
    if res.status != "SUBMITTED" or not res.broker_order_id:
        _print_safe(f"[✗] 未被券商接受: status={res.status}, reason={res.message}")
        return 3
    order_id = res.broker_order_id
    _print_safe(f"    [✓] 已挂单，券商订单号: {order_id} ({t_submit*1000:.1f} ms)")

    t1 = time.perf_counter()
    _print_safe(f"[*] [2/3] 立即撤单 {order_id} ...")
    expected_env = "REAL" if is_real else "SIMULATE"
    cancelled = False
    deadline = time.time() + 8.0
    while time.time() < deadline:
        try:
            broker.cancel_order(order_id, expected_env=expected_env)
        except Exception:
            pass
        time.sleep(0.4)
        executor.reconcile()
        rec = store.intent_by_broker_order(order_id)
        if rec and rec.get("status") in {"CANCELLED", "CANCELLED_ALL"}:
            cancelled = True
            break
    t_cancel = time.perf_counter() - t1
    if cancelled:
        _print_safe(f"    [✓] 撤单闭环确认 ({t_cancel*1000:.1f} ms)")
    else:
        _print_safe("    [!] 8 秒内未闭环，请人工检查挂单状态！")

    time.sleep(0.5)
    executor.reconcile()
    rec = store.intent_by_broker_order(order_id) or {}
    filled = float(rec.get("filled_quantity", 0) or 0)
    status = rec.get("status", "UNKNOWN")

    _print_safe("-" * 60)
    _print_safe(f"  订单号: {order_id}")
    _print_safe(f"  最终状态: {status} | 成交: {filled} (预期 0)")
    _print_safe(f"  下单延迟: {t_submit*1000:.1f} ms | 撤单延迟: {t_cancel*1000:.1f} ms")
    _print_safe("-" * 60)

    if status in {"CANCELLED", "CANCELLED_ALL"} and filled == 0:
        _print_safe("[OK] 挂单-撤单链路闭环健康")
        return 0
    _print_safe("[!] 链路未完全闭环，见上方状态")
    return 4 if not cancelled else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Near-market limit BUY submit + immediate cancel probe")
    parser.add_argument("--tenant", default="futu_real_master")
    parser.add_argument("--symbol", default="US.AAPL")
    parser.add_argument("--below-bid-pct", type=float, default=0.02)
    parser.add_argument("--force-real", action="store_true")
    args = parser.parse_args()
    try:
        sys.exit(run_probe(tenant_id=args.tenant, symbol=args.symbol,
                           below_bid_pct=args.below_bid_pct, force_real=args.force_real))
    finally:
        try:
            close_all_connections()
        except Exception:
            pass


if __name__ == "__main__":
    main()
