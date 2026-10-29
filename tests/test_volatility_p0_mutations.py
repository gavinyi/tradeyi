import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def test_entry_quality_connection_uses_live_quote_and_block_evidence():
    """变异守卫：删除实时覆盖接线或 BLOCKED 盘口证据时必须失败。"""
    source = (Path(__file__).parents[1] / "scripts" / "run_gamma_strategy.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    live_assign = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "live_quality_campaign"
                for target in node.targets)
    )
    live_text = ast.get_source_segment(source, live_assign) or ""
    assert '"spread_pct": float(quote_check["spread_pct"])' in live_text
    for field in (
        "entry_quote_source", "entry_quote_source_time", "entry_quote_captured_at",
        "entry_quote_age_seconds",
    ):
        assert field in live_text

    quality_call = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "volatility_entry_quality_allowed"
        and node.args
    )
    assert isinstance(quality_call.args[0], ast.Name)
    assert quality_call.args[0].id == "live_quality_campaign"

    entry_block = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "record_pretrade_block"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "ENTRY_QUALITY"
    )
    block_text = ast.get_source_segment(source, entry_block) or ""
    for field in (
        "entry_quote_source", "entry_quote_source_time", "entry_quote_captured_at",
        "entry_quote_age_seconds", "entry_quote_spread_pct",
    ):
        assert field in block_text
