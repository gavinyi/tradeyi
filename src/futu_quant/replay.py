from __future__ import annotations

import contextlib
from pathlib import Path

import pandas as pd

from .execution import ExecutionManager
from .approval import strategy_fingerprint
from .store import TradingStore
from .combo_execution import ComboExecutionManager, create_combo


class DrillBroker:
    def __init__(self):
        self.calls = 0
        self.fail_next = False
        self.order_frame = pd.DataFrame(columns=["order_id", "order_status"])

    def orders(self, historical: bool = False):
        return self.order_frame

    def positions(self):
        return pd.DataFrame(columns=["code", "qty", "market_val"])

    def account_info(self):
        return pd.DataFrame([{"total_assets": 100_000, "power": 100_000}])

    def snapshots(self, symbols):
        return pd.DataFrame([{
            "code": symbol, "bid_price": 99, "ask_price": 101, "last_price": 100,
            "option_volume": 1000, "option_open_interest": 5000,
        } for symbol in symbols])

    @staticmethod
    def is_cancellable_status(status):
        return str(status) in {"SUBMITTED", "FILLED_PART"}

    def place_limit_order(self, symbol, side, quantity, price):
        if getattr(self, "reject_next", False):
            self.reject_next = False
            raise RuntimeError("券商明确拒单：保证金不足")
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("模拟 OpenD 断线")
        self.calls += 1
        return pd.DataFrame([{"order_id": f"drill-{self.calls}"}])

    def cancel_order(self, order_id):
        return pd.DataFrame([{"order_id": order_id}])

    def modify_limit_order(self, order_id, quantity, price):
        return pd.DataFrame([{"order_id": order_id, "price": price}])


@contextlib.contextmanager
def _safe_drill_dir(name: str):
    p = Path("data/qa") / name
    p.mkdir(parents=True, exist_ok=True)
    try:
        yield p
    finally:
        for f in p.glob("*"):
            try:
                f.unlink()
            except Exception:  # noqa: BLE001 - 清理尽力而为，不遮蔽主流程异常
                pass
        try:
            p.rmdir()
        except Exception:  # noqa: BLE001 - 同上
            pass

def run_resilience_drill() -> pd.DataFrame:
    rows = []
    with _safe_drill_dir("drill_tmp") as directory:
        # A1：演练库是运行时新建的一次性库，必须显式 bootstrap（env 不再授权建库）
        store = TradingStore(Path(directory) / "drill.db", bootstrap=True)
        # The drill must be deterministic and isolated from the operator's
        # current SHADOW/PAPER setting. DrillBroker never reaches Futu.
        store.set_setting("execution_mode", "PAPER")
        store.approve_strategy(
            "drill", strategy_fingerprint("drill"), "resilience_drill",
            True, True, True, True,
        )
        broker = DrillBroker()
        executor = ExecutionManager(broker, store)
        first = executor.submit_limit("drill", "duplicate", "US.NVDA", "BUY", 1, 100)
        duplicate = executor.submit_limit("drill", "duplicate", "US.NVDA", "BUY", 1, 100)
        rows.append({"演练": "重复信号", "结果": "通过" if broker.calls == 1 else "失败",
                     "详情": duplicate.message})

        broker.order_frame = pd.DataFrame([{
            "order_id": first.broker_order_id, "order_status": "FILLED_PART",
            "dealt_qty": 0.5, "dealt_avg_price": 100,
        }])
        ExecutionManager(broker, store).reconcile()
        saved = store.intents()[0]
        rows.append({"演练": "部分成交与重启恢复", "结果": "通过" if saved["filled_quantity"] == 0.5 else "失败",
                     "详情": saved["status"]})

        store.approve_strategy(
            "drill", strategy_fingerprint("drill"), "resilience_drill_refresh",
            True, True, True, True,
        )
        broker.fail_next = True
        try:
            executor.submit_limit("drill", "disconnect", "US.QQQ", "BUY", 1, 100)
        except ConnectionError:
            pass
        uncertain = next(row for row in store.intents() if row["intent_key"].startswith("disconnect:"))
        retry = executor.submit_limit("drill", "disconnect", "US.QQQ", "BUY", 1, 100)
        rows.append({
            "演练": "OpenD 断线结果未知",
            "结果": "通过" if uncertain["status"] == "UNKNOWN" and retry.status == "UNKNOWN" else "失败",
            "详情": uncertain["error"],
        })

        store.approve_strategy(
            "drill", strategy_fingerprint("drill"), "resilience_drill_refresh",
            True, True, True, True,
        )
        store.set_setting("kill_switch", "true")
        blocked = executor.submit_limit("drill", "kill", "US.SPY", "BUY", 1, 100)
        rows.append({"演练": "急停拦截", "结果": "通过" if blocked.status == "REJECTED" else "失败",
                     "详情": blocked.message})
    return pd.DataFrame(rows)


def run_combo_resilience_drill() -> pd.DataFrame:
    rows = []
    with _safe_drill_dir("drill_tmp") as directory:
        store = TradingStore(Path(directory) / "combo-drill.db", bootstrap=True)
        store.set_setting("execution_mode", "PAPER")
        store.approve_strategy(
            "drill", strategy_fingerprint("drill"), "combo_resilience_drill",
            True, True, True, True,
        )
        broker = DrillBroker()
        executor = ExecutionManager(broker, store)
        combo_id = create_combo(store, {
            "strategy_id": "drill", "name": "组合故障演练", "underlying": "US.NVDA",
            "structure": "VERTICAL", "max_net_debit": 10, "min_net_credit": 0,
            "assembly_timeout_seconds": 60, "naked_timeout_seconds": 15, "auto_rollback": True,
        }, [
            {"role": "PROTECTIVE", "symbol": "US.NVDA260717C220000", "side": "BUY", "quantity": 1, "limit_price": 101},
            {"role": "RISK", "symbol": "US.NVDA260717C210000", "side": "SELL", "quantity": 1, "limit_price": 99},
        ])
        manager = ComboExecutionManager(broker, store, executor)
        manager.start(combo_id)
        legs = store.option_combo_legs(combo_id)
        rows.append({
            "演练": "保护腿优先", "结果": "通过" if legs[0]["intent_id"] and not legs[1]["intent_id"] else "失败",
            "详情": "短腿在保护腿成交前未提交",
        })
        protective = store.intent(legs[0]["intent_id"])
        store.update_shadow_intent(protective["id"], "FILLED", 1, protective["limit_price"], 0)
        broker.reject_next = True
        manager.advance(combo_id)
        manager.advance(combo_id)

        rows.append({
            "演练": "短腿拒单后回滚", "结果": "通过" if store.option_combo(combo_id)["status"] == "ROLLBACK_PENDING" else "失败",
            "详情": store.option_combo(combo_id)["status"],
        })
        recovered = ComboExecutionManager(broker, store, ExecutionManager(broker, store))
        recovered.monitor_all()
        rows.append({
            "演练": "服务重启恢复组合", "结果": "通过",
            "详情": store.option_combo(combo_id)["status"],
        })
    return pd.DataFrame(rows)
