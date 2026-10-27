"""租户启用开关（`tenant_active:<id>`）+ 实盘未启用时预检不误报。

背景（2026-10-28 事故，用户裁定「开了几个就跑几个」）：
- 租户启用状态原先是 `tenant.py` 里的代码常量，界面上关掉实盘账号无效；
- `REAL_TRADING_ENABLED=false` 后实盘 Worker 的预检必然失败 → 被误判成
  「OpenD 掉线」→ 刷 CRITICAL、`opend_consecutive_failures` 累加、零决策。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path("scripts").resolve()))

from futu_quant.tenant import (  # noqa: E402
    DEFAULT_TENANTS,
    active_tenants,
    resolve_tenant_active,
    tenant_active_key,
)


class _FakeStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.heartbeats: list[tuple[str, str, str]] = []
        self.settings: dict[str, str] = {}
        self.events: list[tuple] = []

    def get_setting(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = str(value)
        self.values[key] = str(value)

    def heartbeat(self, name: str, status: str, message: str = "", pid: int | None = None) -> None:
        self.heartbeats.append((name, status, message))

    def operational_event(self, *args, **kwargs) -> None:  # pragma: no cover - 仅记录
        self.events.append((args, kwargs))


class _FakeAlerts:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.recovered: list[tuple] = []

    def send(self, *args, **kwargs) -> None:
        self.sent.append((args, kwargs))

    def recover(self, *args, **kwargs) -> None:
        self.recovered.append((args, kwargs))


class _FakeSettings:
    def __init__(self, real_trading_enabled: bool) -> None:
        self.real_trading_enabled = real_trading_enabled


def test_key_absent_falls_back_to_registry_default():
    store = _FakeStore()
    assert resolve_tenant_active("futu_real_master", store=store) is True
    assert resolve_tenant_active("moomoo_real", store=store) is False


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "FALSE"])
def test_explicit_off_disables_tenant(raw):
    store = _FakeStore({tenant_active_key("futu_real_master"): raw})
    assert resolve_tenant_active("futu_real_master", store=store) is False


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on"])
def test_explicit_on_enables_tenant(raw):
    store = _FakeStore({tenant_active_key("moomoo_real"): raw})
    assert resolve_tenant_active("moomoo_real", store=store) is True


def test_invalid_value_is_fail_closed():
    store = _FakeStore({tenant_active_key("futu_real_master"): "maybe"})
    assert resolve_tenant_active("futu_real_master", store=store) is False


def test_active_tenants_honours_switch():
    store = _FakeStore({tenant_active_key("futu_real_master"): "false"})
    ids = {profile.tenant_id for profile in active_tenants(store=store)}
    assert ids == {"futu_paper_sandbox"}

    # 无 store 时保持旧行为（纯注册表默认值），既有调用方不受影响
    assert "futu_real_master" in {p.tenant_id for p in active_tenants()}


def test_real_trading_disabled_skips_preflight_without_alarms():
    import run_service

    store = _FakeStore()
    alerts = _FakeAlerts()
    settings = _FakeSettings(real_trading_enabled=False)
    tenant = DEFAULT_TENANTS["futu_real_master"]

    ok = run_service.futu_sdk_preflight(
        store, alerts, settings, force=True,
        heartbeat_name="trading_service:futu_real_master", tenant=tenant,
    )

    assert ok is False
    # 心跳是 IDLE（不是 DEGRADED），且没有推任何告警
    assert store.heartbeats == [
        (
            "trading_service:futu_real_master",
            "IDLE",
            "实盘交易未启用（REAL_TRADING_ENABLED=false），跳过 OpenD 预检",
        )
    ]
    assert alerts.sent == []
    # 不得把租户置为连接降级（否则会连坐模拟盘的 BUY）
    assert store.settings.get("connectivity_degraded:futu_real_master") is None
    assert store.settings.get("connectivity_degraded") is None


def test_simulate_tenant_still_runs_preflight(monkeypatch):
    """模拟租户不受 REAL_TRADING_ENABLED 影响，仍走真实预检路径。"""
    import run_service

    called = {}

    class _Done(Exception):
        pass

    def _fake_run(*args, **kwargs):
        called["hit"] = True
        raise _Done

    monkeypatch.setattr(run_service.subprocess, "run", _fake_run)
    store = _FakeStore()
    with pytest.raises(_Done):
        run_service.futu_sdk_preflight(
            store, _FakeAlerts(), _FakeSettings(real_trading_enabled=False),
            force=True, tenant=DEFAULT_TENANTS["futu_paper_sandbox"],
        )
    assert called.get("hit") is True


def test_real_tenant_with_trading_enabled_still_preflights(monkeypatch):
    import run_service

    called = {}

    class _Done(Exception):
        pass

    def _fake_run(*args, **kwargs):
        called["hit"] = True
        raise _Done

    monkeypatch.setattr(run_service.subprocess, "run", _fake_run)
    with pytest.raises(_Done):
        run_service.futu_sdk_preflight(
            _FakeStore(), _FakeAlerts(), _FakeSettings(real_trading_enabled=True),
            force=True, tenant=DEFAULT_TENANTS["futu_real_master"],
        )
    assert called.get("hit") is True
