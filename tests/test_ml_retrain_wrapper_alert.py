from scripts import run_ml_retrain_wrapper as wrapper


class _Store:
    pass


class _Alerts:
    calls = []

    def __init__(self, store):
        self.store = store

    def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_wrapper_pushes_failure_alert(monkeypatch):
    _Alerts.calls = []
    monkeypatch.setattr(wrapper, "TradingStore", _Store)
    monkeypatch.setattr(wrapper, "AlertManager", _Alerts)
    monkeypatch.setattr(
        wrapper, "run_retrain",
        lambda *a, **k: {"status": "FAILED", "reason": "boom", "run_id": 7},
    )
    monkeypatch.setattr(wrapper.sys, "argv", ["run_ml_retrain_wrapper.py", "--kind", "auto"])

    assert wrapper.main() == 1
    assert _Alerts.calls
    args, kwargs = _Alerts.calls[0]
    assert args[:2] == ("ERROR", "ML_RETRAIN_FAILED")
    assert "boom" in args[2]
    assert kwargs["incident_key"] == "ml-retrain:auto"
