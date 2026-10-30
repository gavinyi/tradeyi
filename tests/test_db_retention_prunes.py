"""config_versions / decision_logs 独立短保留（fix/db-retention-tuning）。

两表是大 JSON 热写表：全局 DATA_RETENTION_DAYS=365 对它们形同虚设，
实测分别 223 MB / 318 MB。回归锁定：剪枝只删旧行、新行不受影响。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from futu_quant.store import TradingStore


@pytest.fixture()
def store(tmp_path):
    return TradingStore(str(tmp_path / "retention.db"))


def _insert_config_version(store, config_key, created_at):
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO config_versions(config_key,value_json,actor,reason,created_at)"
            " VALUES(?,?,?,?,?)",
            (config_key, "{}", "test", "regression", created_at.isoformat()),
        )


def _insert_decision(store, created_at):
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO decision_logs(strategy_id,symbol,decision,reason,payload_json,created_at)"
            " VALUES('volatility_long_gamma','US.TEST','HOLD','test','{}',?)",
            (created_at.isoformat(),),
        )


def test_prune_config_versions_keeps_recent(store):
    now = datetime.now(timezone.utc)
    _insert_config_version(store, "VOLATILITY_CAMPAIGNS", now - timedelta(days=40))
    _insert_config_version(store, "VOLATILITY_CAMPAIGNS", now - timedelta(days=1))
    assert store.prune_config_versions(days=30) == 1
    with store.connection() as connection:
        remaining = connection.execute("SELECT COUNT(*) FROM config_versions").fetchone()[0]
    assert remaining == 1


def test_prune_decision_logs_keeps_recent(store):
    now = datetime.now(timezone.utc)
    _insert_decision(store, now - timedelta(days=90))
    _insert_decision(store, now - timedelta(days=2))
    assert store.prune_decision_logs(days=60) == 1
    with store.connection() as connection:
        remaining = connection.execute("SELECT COUNT(*) FROM decision_logs").fetchone()[0]
    assert remaining == 1


def test_retention_cleanup_reports_both_prunes(store, monkeypatch):
    monkeypatch.setenv("CONFIG_VERSION_RETAIN_DAYS", "30")
    monkeypatch.setenv("DECISION_LOG_RETAIN_DAYS", "60")
    from futu_quant.health import retention_cleanup

    result = retention_cleanup(store)
    assert result["config_versions_prune"] == 0
    assert result["decision_logs_prune"] == 0
