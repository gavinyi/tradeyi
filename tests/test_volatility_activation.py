from datetime import datetime, timedelta, timezone

from futu_quant.volatility_activation import (
    build_activation_evidence,
    load_activation_evidence,
    save_activation_evidence,
)


class Store:
    def __init__(self):
        self.values = {}

    def set_setting(self, key, value):
        self.values[key] = value

    def get_setting(self, key, default=""):
        return self.values.get(key, default)


def _evidence(now, *, active=True, trace_id="trace-amd"):
    return build_activation_evidence(
        "US.AMD",
        {
            "active": active,
            "reason": "全部动态波动条件已通过" if active else "量比不足",
            "checks": [{"检查项": "量比", "结果": "达标" if active else "未达标"}],
            "intraday_return": .04,
            "zscore_20d": 2.5,
            "atr_ratio": 1.8,
            "volume_ratio": 2.0 if active else .4,
            "direction_score": 3.0,
            "entry_zscore_target": 2.0,
            "minimum_move_target": .02,
        },
        {"entry_zscore": 2.0, "minimum_underlying_move": .02},
        observed_at=now,
        trace_id=trace_id,
    )


def test_amd_active_replay_consumes_exact_scanner_evidence():
    store = Store()
    now = datetime.now(timezone.utc)
    save_activation_evidence(store, _evidence(now))

    evidence, reason = load_activation_evidence(store, "US.AMD", now=now + timedelta(seconds=30))

    assert reason == "scanner 激活证据有效"
    assert evidence["active"] is True
    assert evidence["trace_id"] == "trace-amd"
    assert evidence["profile"]["entry_zscore"] == 2.0
    assert evidence["metrics"]["zscore_20d"] == 2.5


def test_avgo_stale_replay_is_fail_closed_with_reason():
    store = Store()
    now = datetime.now(timezone.utc)
    evidence = _evidence(now - timedelta(seconds=901), trace_id="trace-avgo")
    evidence["symbol"] = "US.AVGO"
    save_activation_evidence(store, evidence)

    loaded, reason = load_activation_evidence(store, "US.AVGO", now=now)

    assert loaded is None
    assert "已过期" in reason


def test_missing_activation_evidence_is_fail_closed():
    evidence, reason = load_activation_evidence(Store(), "US.AVGO", now=datetime.now(timezone.utc))

    assert evidence is None
    assert reason == "缺少 scanner 激活证据"
