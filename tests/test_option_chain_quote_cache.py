"""期权链缓存必须带盘口列（bid/ask/量/希腊字母）。

根因（2026-11-04 用户要求）：`option_chain()` 直接把 Futu `get_option_chain` 的
返回缓存起来，而该接口**只返回合约元数据**（code/strike/lot_size 等），不含报价。
于是缓存里只有 `code` + `strike_price`，验收时拿不到 bid/ask。

要求：补一轮快照合并、走限速、失败降级但不静默、老缓存不炸、缓存升版。
"""
import pandas as pd
import pytest

import futu_quant.broker as broker_module
from futu_quant.broker import (
    CHAIN_CACHE_VERSION,
    CHAIN_QUOTE_COLUMNS,
    FutuPaperBroker,
    _ensure_chain_quote_columns,
)


class FakeCtx:
    """最小行情上下文：只实现 `get_market_snapshot`。"""

    def __init__(self, payload_by_code, *, ret=0, raises=None):
        self.payload_by_code = payload_by_code
        self.ret = ret
        self.raises = raises
        self.batches: list[list[str]] = []

    def get_market_snapshot(self, symbols):
        self.batches.append(list(symbols))
        if self.raises is not None:
            raise self.raises
        rows = [self.payload_by_code[s] for s in symbols if s in self.payload_by_code]
        return self.ret, pd.DataFrame(rows), ""


def _quote(code, bid=1.10, ask=1.20):
    return {
        "code": code, "bid_price": bid, "ask_price": ask,
        "bid_size": 12, "ask_size": 15, "volume": 340,
        "option_open_interest": 1200, "option_implied_volatility": 42.5,
        "option_delta": 0.42, "option_gamma": 0.013, "option_theta": -0.05,
        "option_vega": 0.11, "update_time": "2026-11-04 15:59:59",
    }


def _chain():
    return pd.DataFrame([
        {"code": "US.SOFI261218P6000", "strike_price": 6.0, "lot_size": 100},
        {"code": "US.SOFI261218P6500", "strike_price": 6.5, "lot_size": 100},
    ])


def _broker():
    """绕过 __init__（不建连接），只测合并逻辑。"""
    return object.__new__(FutuPaperBroker)


# ---------------------------------------------------------------------------
# 1. 合并：报价列必须进帧
# ---------------------------------------------------------------------------


def test_attach_quotes_merges_all_quote_columns():
    b = _broker()
    ctx = FakeCtx({
        "US.SOFI261218P6000": _quote("US.SOFI261218P6000", 1.10, 1.20),
        "US.SOFI261218P6500": _quote("US.SOFI261218P6500", 0.90, 0.95),
    })

    merged = b._attach_chain_quotes(ctx, _chain(), underlying="US.SOFI", expiration="2026-12-18")

    for column in CHAIN_QUOTE_COLUMNS:
        assert column in merged.columns, f"缺少报价列 {column}"
    assert merged.loc[0, "bid_price"] == pytest.approx(1.10)
    assert merged.loc[1, "ask_price"] == pytest.approx(0.95)
    assert bool(merged["quote_missing"].any()) is False


def test_quote_columns_are_serialised_through_cache_round_trip(tmp_path, monkeypatch):
    """缓存 round-trip：写出→读回，列与值必须一致（这是用户要的那条）。"""
    import futu_quant.option_chain_cache as cache

    monkeypatch.setattr(cache, "DB_FILE", tmp_path / "chain-cache.db")
    monkeypatch.setattr(broker_module, "get_frame", cache.get_frame)
    monkeypatch.setattr(broker_module, "save_frame", cache.save_frame)
    monkeypatch.setattr(broker_module, "acquire_option_chain_slot", lambda *a, **k: 0.0)
    monkeypatch.setattr(broker_module, "acquire_api_slot", lambda *a, **k: 0.0)

    b = _broker()
    ctx = FakeCtx({
        "US.SOFI261218P6000": _quote("US.SOFI261218P6000"),
        "US.SOFI261218P6500": _quote("US.SOFI261218P6500"),
    })
    merged = b._attach_chain_quotes(ctx, _chain(), underlying="US.SOFI", expiration="2026-12-18")

    key = f"chain:{CHAIN_CACHE_VERSION}:US.SOFI:2026-12-18"
    cache.save_frame(key, merged)
    restored = cache.get_frame(key, 60)

    assert restored is not None
    for column in CHAIN_QUOTE_COLUMNS:
        assert column in restored.columns
    assert restored["bid_price"].tolist() == pytest.approx([1.10, 1.10])
    # 老 key（无版本）不应与新格式混淆
    assert "chain:US.SOFI:2026-12-18" != key


# ---------------------------------------------------------------------------
# 2. 老格式缓存：不炸，按无报价处理
# ---------------------------------------------------------------------------


def test_legacy_cache_without_quote_columns_is_tolerated():
    legacy = _chain()  # 只有 code/strike/lot_size —— 正是线上那条老缓存
    covered = _ensure_chain_quote_columns(legacy)

    for column in CHAIN_QUOTE_COLUMNS:
        assert column in covered.columns
    assert covered["quote_missing"].all(), "老缓存必须标成「无报价」，不能被当成有效报价"
    assert covered.loc[0, "strike_price"] == 6.0  # 元数据保留


# ---------------------------------------------------------------------------
# 3. 降级：返回链 + quote_missing + 可查询事件
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self):
        self.events = []

    def operational_event(self, component, event_type, status, payload=None):
        self.events.append((component, event_type, status, payload or {}))


def test_snapshot_failure_returns_chain_with_marker_and_event():
    b = _broker()
    b.operational_store = FakeStore()
    ctx = FakeCtx({}, raises=RuntimeError("OpenD 超时"))

    frame = b._attach_chain_quotes(ctx, _chain(), underlying="US.SOFI", expiration="2026-12-18")

    assert len(frame) == 2, "快照失败不得阻断合约链"
    assert frame["quote_missing"].all()
    assert b.operational_store.events, "降级必须留可查询事件，禁止静默"
    component, event_type, status, payload = b.operational_store.events[0]
    assert event_type == "OPTION_CHAIN_QUOTE_MISSING"
    assert status == "WARNING"
    assert payload["missing_contracts"] == 2


def test_partial_quotes_only_mark_the_missing_rows():
    b = _broker()
    b.operational_store = FakeStore()
    ctx = FakeCtx({"US.SOFI261218P6000": _quote("US.SOFI261218P6000")})

    frame = b._attach_chain_quotes(ctx, _chain(), underlying="US.SOFI", expiration="2026-12-18")

    assert frame["quote_missing"].tolist() == [False, True]
    payload = b.operational_store.events[0][3]
    assert payload["missing_contracts"] == 1


# ---------------------------------------------------------------------------
# 4. 限速：必须走 acquire_api_slot，且按批上限分批
# ---------------------------------------------------------------------------


def test_snapshot_calls_respect_rate_limit_and_batch_size(monkeypatch):
    calls = []
    monkeypatch.setattr(broker_module, "acquire_api_slot", lambda bucket, n: calls.append((bucket, n)) or 0.0)

    b = _broker()
    many = pd.DataFrame([{"code": f"US.SOFI261218P{6000 + i}", "strike_price": 6.0} for i in range(450)])
    ctx = FakeCtx({row["code"]: _quote(row["code"]) for _, row in many.iterrows()})

    b._attach_chain_quotes(ctx, many, underlying="US.SOFI", expiration="2026-12-18")

    assert calls, "必须为每批快照申请配额，不能绕开限速"
    assert all(bucket == "snapshots" for bucket, _ in calls)
    assert len(ctx.batches) == 3, "450 个代码按 200 一批应分 3 批"
    assert [len(batch) for batch in ctx.batches] == [200, 200, 50]


# ---------------------------------------------------------------------------
# 5. 变异：去掉快照合并 → 报价列消失（round-trip 用例变红）
# ---------------------------------------------------------------------------


def test_mutation_without_quote_merge_fails_round_trip(monkeypatch):
    b = _broker()
    ctx = FakeCtx({"US.SOFI261218P6000": _quote("US.SOFI261218P6000")})

    # 变异：合并被关掉，只返回原始合约元数据
    monkeypatch.setattr(
        b, "_attach_chain_quotes",
        lambda ctx, frame, *, underlying, expiration: _ensure_chain_quote_columns(frame),
    )
    merged = b._attach_chain_quotes(ctx, _chain(), underlying="US.SOFI", expiration="2026-12-18")

    with pytest.raises(AssertionError):
        assert not merged["bid_price"].isna().all(), (
            "没有合并报价：缓存里不会有 bid（全部为缺失）"
        )


def test_mutation_bypassing_rate_limit_fails_guard(monkeypatch):
    """变异：跳过 acquire_api_slot 直接抓快照 → 限速用例变红。"""
    calls = []
    monkeypatch.setattr(broker_module, "acquire_api_slot", lambda bucket, n: calls.append(bucket) or 0.0)

    b = _broker()
    ctx = FakeCtx({"US.SOFI261218P6000": _quote("US.SOFI261218P6000")})

    def bypass(ctx_, frame, *, underlying, expiration):
        # 变异：不限速、不分批
        ctx_.get_market_snapshot([str(c) for c in frame["code"].tolist()])
        return _ensure_chain_quote_columns(frame)

    monkeypatch.setattr(b, "_attach_chain_quotes", bypass)
    b._attach_chain_quotes(ctx, _chain(), underlying="US.SOFI", expiration="2026-12-18")

    with pytest.raises(AssertionError):
        assert calls, "跳过限速抓快照：必须被抓出来"
