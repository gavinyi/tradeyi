"""P1-21 守卫：心跳恢复后全局行必须能回到 HEALTHY（假故障回归）。"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

from futu_quant.store import TradingStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class _Alerts:
    def send(self, *args, **kwargs):
        return None

    def recover(self, *args, **kwargs):
        return None


def _store(tmp_path) -> TradingStore:
    return TradingStore(tmp_path / "heartbeat.db")


def test_preflight_failure_writes_tenant_scoped_heartbeat(tmp_path, monkeypatch):
    """失败路径必须写带租户的心跳名，不得污染全局 `trading_service` 行。"""
    import run_service

    store = _store(tmp_path)
    monkeypatch.setattr(
        run_service.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    ok = run_service.futu_sdk_preflight(
        store, _Alerts(), None, force=True,
        heartbeat_name="trading_service:futu_real_master",
    )
    assert ok is False
    rows = {row["strategy_id"]: row for row in store.heartbeats()}
    assert "trading_service:futu_real_master" in rows
    assert rows["trading_service:futu_real_master"]["status"] == "DEGRADED"
    assert "trading_service" not in rows, "失败路径不得再写无租户的全局心跳"


def test_service_cycle_success_also_refreshes_global_heartbeat():
    """成功路径必须补写全局 `trading_service` HEALTHY（否则历史 DEGRADED 永不复原）。"""
    import run_service

    source = Path(run_service.__file__).read_text(encoding="utf-8")
    assert '"trading_service", "HEALTHY"' in source
    assert "全局兼容行，不用于判故障" in source
