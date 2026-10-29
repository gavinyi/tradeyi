from datetime import datetime, timedelta, timezone

import pandas as pd

from futu_quant.volatility_chain import campaign_intent_chain_status
from futu_quant.volatility_optimizer import complete_volatility_activation


def test_complete_activation_rechecks_spy_independence_gate(monkeypatch):
    monkeypatch.setattr(
        "futu_quant.volatility_optimizer.volatility_regime",
        lambda *_a, **_k: {"active": True, "reason": "基础条件通过", "intraday_return": .02},
    )
    result = complete_volatility_activation(
        {}, pd.DataFrame(), {"entry_zscore": 2.0, "minimum_underlying_move": .02}, .019,
    )
    assert result["active"] is False
    assert result["market_independent"] is False
    assert "随大盘同步" in result["reason"]


def test_chain_time_comparison_normalizes_z_and_offsets():
    now = datetime(2026, 10, 29, 15, 20, tzinfo=timezone.utc)
    campaign = {
        "code": "US.AMD261106C200000", "activation_trace_id": "trace-amd",
        "auto_created_at": "2026-10-29T15:00:00Z",
    }
    # 同一绝对时刻的 +08:00 写法必须被识别为创建之后，不能按字符串误判。
    intent = {
        "tenant_id": "paper-a", "strategy_id": "volatility_long_gamma",
        "symbol": campaign["code"], "created_at": "2026-10-29T23:00:01+08:00",
    }
    broken, reason = campaign_intent_chain_status(
        campaign, now=now, intents=[intent], decisions=[], tenant_id="paper-a",
        timeout_seconds=60,
    )
    assert broken is False
    assert reason == "已形成订单意图"


def test_chain_rejects_naive_row_time_instead_of_string_comparing():
    now = datetime(2026, 10, 29, 15, 20, tzinfo=timezone.utc)
    campaign = {
        "code": "US.AMD261106C200000", "activation_trace_id": "trace-amd",
        "auto_created_at": (now - timedelta(minutes=20)).isoformat(),
    }
    naive_intent = {
        "tenant_id": "paper-a", "strategy_id": "volatility_long_gamma",
        "symbol": campaign["code"], "created_at": "2099-01-01T00:00:00",
    }
    broken, _ = campaign_intent_chain_status(
        campaign, now=now, intents=[naive_intent], decisions=[], tenant_id="paper-a",
        timeout_seconds=60,
    )
    assert broken is True
