"""入场质量闸门 `checks` 落库（观测缺失修复）。

背景：`volatility_entry_quality_allowed` 一直返回结构化 `checks`，但调用方只
拿它拼中文 reason，**不落库** → 事后只能从一句中文里猜「这张合约卡在哪一条」。

本组测试钉住三件事：
1. 通过时落一条 `VOLATILITY_ENTRY_QUALITY`，`allowed=True`；
2. 拒绝时落库，且 `checks` 全量 + 生效阈值齐全；
3. 字段缺失 / 无 store / store 抛错时**不抛异常**（观测不能反噬交易）。
"""

from __future__ import annotations

import json

from futu_quant.store import TradingStore
from futu_quant.volatility_optimizer import volatility_entry_quality_allowed


def _campaign(**overrides):
    base = {
        "volatility_quality_score": 80.0,
        "minimum_entry_quality_score": 60.0,
        "model_expected_return": 0.04,
        "spread_pct": 1.0,
        "maximum_entry_spread_pct": 10.0,
        "option_open_interest": 5000,
        "volume": 5000,
        "minimum_entry_open_interest": 100,
        "minimum_entry_volume": 10,
        "tenant_id": "futu_paper_sandbox",
        "strategy_id": "volatility_long_gamma",
        "symbol": "US.QQQ",
        "code": "US.QQQ251121C400000",
    }
    base.update(overrides)
    return base


def _records(store):
    return [
        item for item in store.audit_events()
        if item["event_type"] == "VOLATILITY_ENTRY_QUALITY"
    ]


def test_passing_judgement_is_persisted(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    ok, _reason, checks = volatility_entry_quality_allowed(_campaign(), store=store)
    assert ok is True

    rows = _records(store)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["allowed"] is True
    assert payload["tenant_id"] == "futu_paper_sandbox"
    assert payload["strategy_id"] == "volatility_long_gamma"
    assert payload["symbol"] == "US.QQQ"
    assert payload["code"] == "US.QQQ251121C400000"
    assert payload["checks"] == checks


def test_rejected_judgement_persists_all_checks_and_thresholds(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    # 质量分低 + 价差超限 + OI 不足 → 三条同时不通过
    campaign = _campaign(
        volatility_quality_score=10.0,
        spread_pct=99.0,
        option_open_interest=1,
    )
    ok, reason, checks = volatility_entry_quality_allowed(campaign, store=store)
    assert ok is False
    assert "入场质量闸门未通过" in reason

    rows = _records(store)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["allowed"] is False
    # 全量 checks 都在
    for key in ("quality_score", "spread_pct", "open_interest", "volume",
                "expected_return_to_cost", "min_return_cost_ratio"):
        assert key in payload["checks"]
    # 生效阈值齐全
    thresholds = payload["effective_thresholds"]
    for key in ("minimum_quality_score", "maximum_spread_pct",
                "minimum_open_interest", "min_return_cost_ratio",
                "minimum_model_expected_return"):
        assert key in thresholds


def test_missing_fields_and_bad_store_never_raise(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    # 空 campaign：字段全缺 → 判定照跑、记录照写、不抛异常
    ok, reason, checks = volatility_entry_quality_allowed({}, store=store)
    assert isinstance(ok, bool)
    assert isinstance(reason, str)
    assert isinstance(checks, dict)
    assert len(_records(store)) == 1

    # 无 store：不落库、不抛异常
    ok2, _r2, _c2 = volatility_entry_quality_allowed({})
    assert isinstance(ok2, bool)

    # store.audit 抛错：判定结果不受影响
    class Boom:
        def audit(self, *args, **kwargs):
            raise RuntimeError("boom")

    ok3, _r3, _c3 = volatility_entry_quality_allowed(_campaign(), store=Boom())
    assert ok3 is True


def test_call_path_distinguishes_entry_from_review(tmp_path):
    """入场路径与复核路径必须可区分。

    复核循环每 300 秒无条件写一条；若不区分，真出现 BUY 时入场那条会被
    复核记录淹没，恰好丢掉要回答的问题。
    """
    store = TradingStore(tmp_path / "t.db")
    volatility_entry_quality_allowed(_campaign(), store=store, call_path="review")
    volatility_entry_quality_allowed(_campaign(), store=store, call_path="entry")

    rows = _records(store)
    assert len(rows) == 2
    paths = [json.loads(item["payload_json"])["call_path"] for item in rows]
    assert set(paths) == {"review", "entry"}
    # 能按 call_path 精确筛出入场那条
    entry = [json.loads(i["payload_json"]) for i in rows
             if json.loads(i["payload_json"])["call_path"] == "entry"]
    assert len(entry) == 1


def test_payload_declares_gate_identity_and_iv_coverage(tmp_path):
    """留痕必须自带闸门身份：这是质量分闸门，**不覆盖 IV 百分位闸门**。

    2026-10-28 曾拿 `allowed=true` 去否决 `volatility_structures.py:87`
    的 IV 闸门，属越界外推；payload 里写死标识防止后人重犯。
    """
    store = TradingStore(tmp_path / "t.db")
    volatility_entry_quality_allowed(_campaign(), store=store)
    payload = json.loads(_records(store)[0]["payload_json"])
    assert payload["gate"] == "ENTRY_QUALITY"
    assert payload["covers_iv_percentile"] is False

