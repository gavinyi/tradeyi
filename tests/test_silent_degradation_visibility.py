"""批 A：静默降级清零的守卫（2026-10-26）。

对应 P0：
1. 任务包装器不得把内部 DEGRADED 覆盖成 HEALTHY（`run_service.py` run_task）；
2. 采集缺合约不得静默 `continue`；
3. `snapshots_tolerant` 丢 symbol 必须可查；
4. 审计/告警、历史委托拉取失败不得 `except: pass`。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging
from pathlib import Path
import sys

import pandas as pd
import pytest

from futu_quant.alerts import AlertManager
from futu_quant.execution import ExecutionManager
from futu_quant.option_history import collect_option_snapshots
from futu_quant.store import TradingStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_service import internal_degraded_since, run_task  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 包装器不得覆盖内部 DEGRADED
# ---------------------------------------------------------------------------


def test_internal_degraded_survives_success_wrapper(tmp_path: Path):
    store = TradingStore(tmp_path / "trading.db")
    alerts = AlertManager(store)

    def callback() -> None:
        # 模拟采集任务内部：先写 RUNNING（包装器已写），再报 DEGRADED
        store.heartbeat("option_history", "DEGRADED", "采集 357 个合约；25 条降级")

    assert run_task(store, alerts, "option_history", callback) is True

    heartbeat = store.heartbeat_status("option_history")
    assert heartbeat["status"] == "DEGRADED", "内部 DEGRADED 被成功路径覆盖成 HEALTHY"
    assert "25 条降级" in heartbeat["message"]
    run = store.task_runs(limit=1)[0]
    assert run["status"] == "DEGRADED"
    assert "25 条降级" in run["message"]


def test_stale_degraded_from_previous_run_is_recovered(tmp_path: Path):
    """上一轮留下的 DEGRADED 不能被当成「本轮降级」，否则任务永远恢复不了。"""
    store = TradingStore(tmp_path / "trading.db")
    alerts = AlertManager(store)
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.heartbeat("option_history", "DEGRADED", "上一轮的旧降级")
    # 把 updated_at 改到 1 小时前
    with store.connection() as connection:
        connection.execute(
            "UPDATE strategy_heartbeats SET updated_at=? WHERE strategy_id='option_history'",
            (stale,),
        )

    assert run_task(store, alerts, "option_history", lambda: None) is True
    heartbeat = store.heartbeat_status("option_history")
    assert heartbeat["status"] == "HEALTHY"
    assert store.task_runs(limit=1)[0]["status"] == "SUCCESS"


def test_internal_degraded_then_healthy_reports_worst(tmp_path: Path):
    """内层「先 DEGRADED 后 HEALTHY」也要按最差上报，否则最后一条 HEALTHY 等于洗白。"""
    store = TradingStore(tmp_path / "trading.db")
    alerts = AlertManager(store)

    def callback() -> None:
        store.heartbeat("option_history", "DEGRADED", "采集中：25 条降级")
        store.heartbeat("option_history", "HEALTHY", "后来补写一条健康")

    assert run_task(store, alerts, "option_history", callback) is True
    heartbeat = store.heartbeat_status("option_history")
    assert heartbeat["status"] == "DEGRADED", "最后一条内层 HEALTHY 把 DEGRADED 洗白了"
    assert "25 条降级" in heartbeat["message"]
    assert store.task_runs(limit=1)[0]["status"] == "DEGRADED"


def test_internal_degraded_since_handles_missing_and_naive_timestamps():
    assert internal_degraded_since(None, "2026-10-26T00:00:00+00:00") is False
    assert internal_degraded_since({"status": "HEALTHY", "updated_at": "x"}, "y") is False
    naive = {"status": "ERROR", "updated_at": "2026-10-26T00:00:00"}
    assert internal_degraded_since(naive, "2026-10-25T23:00:00+00:00") is True
    assert internal_degraded_since({
        "status": "DEGRADED", "updated_at": "2026-10-25T00:00:00+00:00",
    }, "2026-10-26T00:00:00+00:00") is False


# ---------------------------------------------------------------------------
# 2. 采集缺合约：审计 + 心跳降级 + 计数
# ---------------------------------------------------------------------------


def test_option_history_missing_contract_is_audited(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "trading.db")
    monkeypatch.setattr(
        "futu_quant.option_history.load_gamma_campaigns",
        lambda: [{"code": "US.AAPL260720C300000"}, {"code": "US.NVDA260720C200000"}],
    )
    monkeypatch.setattr(
        "futu_quant.option_history.load_all_volatility_campaigns", lambda: []
    )
    from datetime import timezone
    fresh = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).isoformat()

    class Broker:
        @staticmethod
        def positions():
            return pd.DataFrame()

        @staticmethod
        def snapshots(symbols):
            # 只返回 AAPL，NVDA 合约缺失 → 必须可见
            return pd.DataFrame([
                {
                    "code": "US.AAPL260720C300000", "bid_price": 30, "ask_price": 30.5,
                    "option_implied_volatility": 0.4, "option_delta": 0.8,
                    "option_open_interest": 100, "volume": 10, "update_time": fresh,
                },
                {"code": "US.AAPL", "last_price": 330},
                {"code": "US.NVDA", "last_price": 180},
            ])

    result = collect_option_snapshots(Broker(), store)
    assert result["missing"] == 1
    audits = [row for row in store.audit_events(limit=20)
              if row["event_type"] == "OPTION_SNAPSHOT_MISSING"]
    assert audits, "缺合约没有落 OPTION_SNAPSHOT_MISSING"
    payload = audits[0]["payload_json"]
    assert "US.NVDA260720C200000" in payload and "snapshot_absent" in payload
    heartbeat = store.heartbeat_status("option_history")
    assert heartbeat["status"] == "DEGRADED"
    assert "缺失 1" in heartbeat["message"]


def test_option_history_sixty_percent_usable_threshold_guard(tmp_path, monkeypatch):
    """Bug 治理守卫：
    1. 正常市场微小降级（如 10 个合约中 8 个可用，比例 80% >= 60% 且无 missing）：
       必须报告 HEALTHY，绝不触发 DEGRADED 误报稀释真故障；
    2. 可用比例严重降低（如 10 个中仅 4 个可用，比例 40% < 60%）：
       必须坚决触发 DEGRADED 心跳告警；
    3. 若 records 完全为空，必须判定为 DEGRADED。
    """
    store = TradingStore(tmp_path / "opt_hist_guard.db")
    from datetime import timezone
    fresh = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).isoformat()

    # 1. 场景 A: 80% 可用 -> HEALTHY
    test_codes = [f"US.AAPL260720C{300000 + i * 1000}" for i in range(10)]
    monkeypatch.setattr(
        "futu_quant.option_history.load_gamma_campaigns",
        lambda: [{"code": c} for c in test_codes],
    )
    monkeypatch.setattr("futu_quant.option_history.load_all_volatility_campaigns", lambda: [])

    class HealthyBroker:
        @staticmethod
        def positions():
            return pd.DataFrame()

        @staticmethod
        def snapshots(symbols):
            records = [{"code": "US.AAPL", "last_price": 200.0}]
            for i, s in enumerate(symbols):
                if s == "US.AAPL":
                    continue
                # 前 8 个正常有双边价，后 2 个为深虚无 bid
                bid = 5.0 if i < 8 else 0.0
                ask = 5.5 if i < 8 else 0.0
                records.append({
                    "code": s, "bid_price": bid, "ask_price": ask,
                    "option_implied_volatility": 0.4, "option_delta": 0.5,
                    "option_open_interest": 100, "volume": 10, "update_time": fresh,
                })
            return pd.DataFrame(records)

    collect_option_snapshots(HealthyBroker(), store)
    assert store.heartbeat_status("option_history")["status"] == "HEALTHY"

    # 2. 场景 B: 仅 40% 可用 (< 60%) -> DEGRADED
    class DegradedBroker:
        @staticmethod
        def positions():
            return pd.DataFrame()

        @staticmethod
        def snapshots(symbols):
            records = [{"code": "US.AAPL", "last_price": 200.0}]
            for i, s in enumerate(symbols):
                if s == "US.AAPL":
                    continue
                bid = 5.0 if i < 4 else 0.0
                ask = 5.5 if i < 4 else 0.0
                records.append({
                    "code": s, "bid_price": bid, "ask_price": ask,
                    "option_implied_volatility": 0.4, "option_delta": 0.5,
                    "option_open_interest": 100, "volume": 10, "update_time": fresh,
                })
            return pd.DataFrame(records)

    collect_option_snapshots(DegradedBroker(), store)
    assert store.heartbeat_status("option_history")["status"] == "DEGRADED"

    # 3. 场景 C: 快照全空 -> 抛出 ValueError 触发异常处理，不能静默 HEALTHY
    class EmptyBroker:
        @staticmethod
        def positions():
            return pd.DataFrame()

        @staticmethod
        def snapshots(symbols):
            return pd.DataFrame()

    with pytest.raises(ValueError, match="期权历史采集没有取得行情快照"):
        collect_option_snapshots(EmptyBroker(), store)




# ---------------------------------------------------------------------------
# 3. snapshots_tolerant 丢 symbol 必须可查
# ---------------------------------------------------------------------------


def _fake_tolerant_broker(monkeypatch, payloads):
    """payloads: {symbol: (ret, data)}；批量调用按 batch 内容模拟。"""
    import futu_quant.broker as broker_module
    from futu import RET_ERROR, RET_OK

    monkeypatch.setattr(broker_module, "acquire_api_slot", lambda *a, **k: None)

    class Ctx:
        def get_market_snapshot(self, batch):
            ok = [s for s in batch if s not in payloads]
            bad = [s for s in batch if s in payloads]
            if bad:
                return RET_ERROR, "not support: " + ",".join(bad)
            return RET_OK, pd.DataFrame([{"code": s} for s in ok])

    broker = object.__new__(broker_module.FutuPaperBroker)

    @contextmanager
    def quote_context():
        yield Ctx()

    broker.quote_context = quote_context
    return broker


def test_snapshots_tolerant_reports_dropped_symbol(monkeypatch, caplog):
    broker = _fake_tolerant_broker(monkeypatch, {"US.BAD"})
    dropped: list[str] = []
    with caplog.at_level(logging.WARNING, logger="futu_quant.broker"):
        frame = broker.snapshots_tolerant(["US.AAPL", "US.BAD"], dropped_out=dropped)

    assert list(frame["code"]) == ["US.AAPL"]
    assert dropped == ["US.BAD"]
    assert any("US.BAD" in record.getMessage() for record in caplog.records), (
        "snapshots_tolerant 丢 symbol 仍无日志"
    )


# ---------------------------------------------------------------------------
# 4. 审计/告警失败、历史委托拉取失败不得 pass
# ---------------------------------------------------------------------------


def test_mark_unknown_logs_when_audit_and_alert_fail(caplog):
    manager = object.__new__(ExecutionManager)

    class Store:
        def intent(self, intent_id):
            return None

        def audit(self, *args, **kwargs):
            raise RuntimeError("audit db locked")

    class Alerts:
        def send(self, *args, **kwargs):
            raise RuntimeError("feishu down")

    manager.store = Store()
    manager.alerts = Alerts()

    with caplog.at_level(logging.ERROR, logger="futu_quant.execution"):
        manager._mark_unknown({"id": 7, "strategy_id": "s1", "symbol": "US.AAPL"},
                              "broker timeout", "ORDER_UNKNOWN")
    assert any("audit db locked" in record.getMessage() for record in caplog.records), (
        "审计/告警失败被静默吞掉"
    )


def test_reconcile_history_orders_failure_is_audited(tmp_path, caplog):
    store = TradingStore(tmp_path / "trading.db")

    class Broker:
        def orders(self, historical=False):
            if historical:
                raise RuntimeError("history orders unavailable")
            return pd.DataFrame(columns=["order_id", "order_status"])

    manager = object.__new__(ExecutionManager)
    manager.broker = Broker()
    manager.store = store
    manager.alerts = AlertManager(store)

    with caplog.at_level(logging.ERROR, logger="futu_quant.execution"):
        assert manager.reconcile() == 0

    assert any("history orders unavailable" in r.getMessage() for r in caplog.records)
    events = [row for row in store.audit_events(limit=20)
              if row["event_type"] == "RECONCILIATION_HISTORY_ORDERS_FAILED"]
    assert events, "历史委托拉取失败没有审计事件"
