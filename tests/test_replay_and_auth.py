from __future__ import annotations

from futu_quant.admin_auth import configured, hash_password, verify
from futu_quant.replay import run_resilience_drill


def test_admin_password_hash(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("secret", iterations=1000))

    assert configured()
    assert verify("secret")
    assert not verify("wrong")


def test_resilience_drill_passes_all_scenarios(monkeypatch):
    monkeypatch.delenv("STRATEGY_BUDGETS", raising=False)
    monkeypatch.setenv("ENFORCE_VIRTUAL_POSITIONS", "false")

    result = run_resilience_drill()

    assert set(result["结果"]) == {"通过"}
