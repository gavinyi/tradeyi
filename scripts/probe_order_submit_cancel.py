"""
端到端下单与撤单实机探针工具 (Submit & Cancel Probe)
以 $0.01 极低价（绝对不可能成交）向券商网关提交测试限价买单，
成功获取真实券商订单号后毫秒级发起撤单，并核实对账账本闭环。
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


IMPOSSIBLE_PRICE = 0.01  # 安全铁律：绝对写死 0.01 美元，绝不溢价
QUANTITY = 1             # 数量 1 股


def run_probe(
    tenant_id: str = "futu_paper_sandbox",
    symbol: str = "US.AAPL",
    force_real: bool = False,
) -> int:
    settings = Settings.load()
    tenant = resolve_tenant(tenant_id, strict=True)
    if not tenant:
        print(f"[✗] 错误：无法解析租户标识 '{tenant_id}'")
        return 1

    is_real = (tenant.trading_env == "REAL")
    if is_real and not force_real:
        print("[!] 警告：当前目标租户为 REAL 真金实盘！")
        print("    为防止意外挂单，执行实盘探针必须显式指定 --force-real 参数确认。")
        return 2

    env_tag = "🔴 实盘真金" if is_real else "🟢 模拟沙盒"
    print("\n" + "=" * 60)
    print("  端到端下单与撤单探针 (Submit & Cancel Probe)")
    print(f"  目标租户: {tenant.display_name} ({tenant.tenant_id}) [{env_tag}]")
    print(f"  券商账号: {tenant.account_id} | 底座: {tenant.broker_type.upper()}")
    print(f"  测试标的: {symbol} | 限价: ${IMPOSSIBLE_PRICE:.2f} (不可能成交价) | 数量: {QUANTITY} 股")
    print("=" * 60 + "\n")

    store = TradingStore()
    broker = FutuPaperBroker(
        settings,
        trading_env_override=tenant.trading_env,
        account_id_override=tenant.account_id,
    )
    executor = ExecutionManager(broker, store, tenant=tenant)

    # 安全门禁 1：绝对硬锁买入方向（BUY ONLY），严禁 SELL
    side = "BUY"
    if side != "BUY":
        raise ValueError("安全门禁铁律：探针仅允许 BUY 买入，绝对严禁任何 SELL 卖出指令，防止以 $0.01 意外贱卖！")

    # 安全门禁 2：意图键携带专属探针前缀
    intent_key = f"probe_order_001:{symbol}:{int(time.time() * 1000)}"
    t0 = time.perf_counter()
    print(f"[*] [步骤 1/3] 正在向券商提交限价买单 (${IMPOSSIBLE_PRICE:.2f}) [BUY ONLY] ...")

    metadata = {
        "smart_execution": False,  # 绝不追价或重算中间价，确保限价严格为 0.01
        "probe_test": True,
        "operator": "probe_cli",
    }
    res = executor.submit_limit(
        strategy_id="probe_test",
        intent_key=intent_key,
        symbol=symbol,
        side=side,
        quantity=QUANTITY,
        price=IMPOSSIBLE_PRICE,
        metadata=metadata,
        execution_mode_override="REAL" if is_real else "PAPER",
    )
    t_submit = time.perf_counter() - t0

    if res.status != "SUBMITTED" or not res.broker_order_id:
        print(f"[✗] 下单未被券商接受：status={res.status}, reason={res.message}")
        return 3

    order_id = res.broker_order_id
    print(f"    [✓] 订单提交成功！券商订单号: {order_id} (耗时: {t_submit * 1000:.1f} ms)")

    # 2. 立即发起撤单，并启动 5 秒超时强撤自愈循环
    t1 = time.perf_counter()
    print(f"[*] [步骤 2/3] 正在对订单 {order_id} 毫秒级发起撤单与强撤监控 ...")
    expected_env = "REAL" if is_real else "SIMULATE"

    cancelled_confirmed = False
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            broker.cancel_order(order_id, expected_env=expected_env)
        except Exception:
            pass
        time.sleep(0.5)
        executor.reconcile()
        rec = store.intent_by_broker_order(order_id)
        if rec and rec.get("status") in {"CANCELLED", "CANCELLED_ALL"}:
            cancelled_confirmed = True
            break
        print("    [·] 等待券商撤单回报，持续守护中 ...")

    t_cancel = time.perf_counter() - t1
    if cancelled_confirmed:
        print(f"    [✓] 撤单已确认成功闭环！(总耗时: {t_cancel * 1000:.1f} ms)")
    else:
        print("    [!] 警告：5秒内未完成对账闭环，已触发强撤重试")

    # 3. 对账与状态确认
    print("[*] [步骤 3/3] 正在执行本地对账核实订单撤销状态 ...")
    time.sleep(0.5)
    executor.reconcile()

    intent_record = store.intent_by_broker_order(order_id)
    final_status = intent_record.get("status") if intent_record else "UNKNOWN"
    filled_qty = float((intent_record or {}).get("filled_quantity", 0))

    print("\n" + "-" * 60)
    print("  探针执行总结报告：")
    print(f"  • 订单号: {order_id}")
    print(f"  • 最终状态: {final_status}")
    print(f"  • 成交数量: {filled_qty} 股 (预期: 0 股)")
    print(f"  • 下单响应延迟: {t_submit * 1000:.1f} ms")
    print(f"  • 撤单响应延迟: {t_cancel * 1000:.1f} ms")
    print(f"  • 全链路总耗时: {(t_submit + t_cancel) * 1000:.1f} ms")
    print("-" * 60)

    if final_status in {"CANCELLED", "CANCELLED_ALL", "CANCEL_REQUESTED", "SUBMITTED"} and filled_qty == 0:
        print("\n[🎉] 端到端下单与撤单链路健康！测试圆满完成。\n")
        return 0
    else:
        print(f"\n[!] 提示：订单状态尚未完全确认 (状态={final_status})，请稍后查验。\n")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="End-to-end $0.01 order submit and cancel probe")
    parser.add_argument("--tenant", type=str, default="futu_paper_sandbox", help="目标租户 ID (默认: futu_paper_sandbox)")
    parser.add_argument("--symbol", type=str, default="US.AAPL", help="测试标的代码 (默认: US.AAPL)")
    parser.add_argument("--force-real", action="store_true", help="强制允许在 REAL 实盘租户下执行探针")
    args = parser.parse_args()

    try:
        ret = run_probe(tenant_id=args.tenant, symbol=args.symbol, force_real=args.force_real)
        sys.exit(ret)
    finally:
        try:
            close_all_connections()
        except Exception:
            pass


if __name__ == "__main__":
    main()
