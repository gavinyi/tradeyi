from datetime import datetime, timedelta, timezone

import pandas as pd

from futu_quant.volatility_chain import campaign_intent_chain_status
from futu_quant.volatility_optimizer import complete_volatility_activation
from scripts import run_gamma_strategy as gamma


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


def test_live_futu_book_overrides_cached_campaign_spread_and_keeps_evidence(monkeypatch):
    """旧 campaign 价差合格也不能放行；入场门禁必须采用 Futu 最新盘口。"""
    code = "US.AMD261106C200000"
    underlying = "US.AMD"
    source_time = "2026-10-29 11:19:55"
    captured_at = datetime(2026, 10, 29, 15, 20, tzinfo=timezone.utc)

    class Broker:
        def snapshots(self, symbols):
            rows = []
            for symbol in symbols:
                if symbol == code:
                    rows.append({
                        "code": symbol, "bid_price": 1.0, "ask_price": 1.4,
                        "last_price": 1.2, "update_time": source_time,
                    })
                else:
                    rows.append({
                        "code": symbol, "bid_price": 199.9, "ask_price": 200.1,
                        "last_price": 200.0, "update_time": source_time,
                    })
            return pd.DataFrame(rows)

    # 冻结函数内部采集时钟，使证据可精确断言；禁用重试等待。
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return captured_at.astimezone(tz) if tz else captured_at.replace(tzinfo=None)

    monkeypatch.setattr(gamma, "datetime", FrozenDateTime)
    _option, _owner, evidence = gamma.refreshed_trade_quotes(
        Broker(), code, underlying, captured_at,
        maximum_spread_pct=12.0, retries=0,
    )

    # campaign 缓存的 3% 本可放行，但 Futu 实时 1.0/1.4 = 33.33%，必须拦截。
    campaign = {"spread_pct": 3.0}
    assert campaign["spread_pct"] < 12.0
    assert evidence["book_ok"] is False
    assert evidence["spread_pct"] > 12.0
    assert evidence["quote_source"] == "FUTU_OPEND_SNAPSHOT"
    assert evidence["option_source_time"] == source_time
    assert evidence["captured_at"] == captured_at.isoformat()
    assert evidence["option_age_seconds"] is not None
    assert evidence["option_age_seconds"] >= 0
