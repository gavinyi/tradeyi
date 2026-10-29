from __future__ import annotations

from pathlib import Path

import pandas as pd

from futu_quant.health import system_health


ROOT = Path(__file__).resolve().parents[1]
SERVICE = (ROOT / "scripts" / "run_service.py").read_text(encoding="utf-8")
HEALTH = (ROOT / "src" / "futu_quant" / "health.py").read_text(encoding="utf-8")


class Store:
    def __init__(self):
        self.audits = []

    def audit(self, event, payload, actor):
        self.audits.append((event, payload, actor))

    def integrity_check(self):  # pragma: no cover - must never be called
        raise AssertionError("轻量健康检查不得执行整库 integrity_check")

    def backup(self, _path):  # pragma: no cover - must never be called
        raise AssertionError("轻量健康检查不得执行备份")

    def cleanup(self, _cutoff):  # pragma: no cover - must never be called
        raise AssertionError("轻量健康检查不得执行清理")


class Broker:
    settings = type("Settings", (), {"watchlist": ()})()

    @staticmethod
    def accounts():
        return pd.DataFrame([{"account": "paper"}])


def test_system_health_is_lightweight_and_audited():
    store = Store()

    result = system_health(store, Broker())

    assert result == {"opend": "OK", "quote": "UNKNOWN"}
    assert store.audits[0][0] == "HEALTH_CHECK"


def test_heavy_maintenance_has_independent_tasks_and_intervals():
    assert '"system_health": 30' in SERVICE
    assert '"database_integrity": 180' in SERVICE
    assert '"database_backup": 300' in SERVICE
    assert '"retention_cleanup": 180' in SERVICE
    assert 'due(store, "last_run:system_health", 300, now)' in SERVICE
    assert 'due(store, "last_run:database_integrity", 20 * 60 * 60, now)' in SERVICE
    assert 'due(store, "last_run:database_backup", 20 * 60 * 60, now)' in SERVICE
    assert 'due(store, "last_run:retention_cleanup", 20 * 60 * 60, now)' in SERVICE
    assert 'run_task(store, alerts, "database_integrity"' in SERVICE
    assert 'run_task(store, alerts, "database_backup"' in SERVICE
    assert 'run_task(store, alerts, "retention_cleanup"' in SERVICE
    for task in ("database_integrity", "database_backup", "retention_cleanup"):
        assert f'if run_task(store, alerts, "{task}"' in SERVICE
        block = SERVICE.split(f'if due(store, "last_run:{task}"', 1)[1].split("\n    if due(", 1)[0]
        assert block.index("if run_task(") < block.index(f'mark_run(store, "last_run:{task}"')


def test_lightweight_function_contains_no_heavy_database_operations():
    body = HEALTH.split("def system_health", 1)[1].split("def database_integrity_check", 1)[0]
    assert ".integrity_check(" not in body
    assert ".backup(" not in body
    assert ".cleanup(" not in body
