"""候选质量过滤守卫（P0-5）。

每条判据对应设计 §8 的一条验收，且**必须能失败**：

* 阈值默认 = 现值（500/12/10）→ 接管不得改变行为；
* **链式校验 fail-closed**（`soft ≤ hard ≤ entry`）→ 防「候选层放行、入场层拦住」的不可解释 BLOCK；
* **配置非法 → 拒解析**（不静默回落默认值）；
* **报价停滞 >180s → 硬排除**，且 `age_seconds` 记**实测值**；
  注入点 = 设计 §8 验收 2 指定的 `stub_quotes_with_age`（否则真实券商构造不出"1250 秒前"的报价）；
* **软降权 ≠ 硬排除** —— 实现成硬排除即失败；
* 排除留痕**带租户 + 实测值**，且**绝不写 `runtime_config`**；
* `candidate_exclusions` 表**走版本化迁移**（存量库也要有）—— 今天刚栽过「只写新建库分支」。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from futu_quant.candidate_quality import (
    PHASE_HARD_EXCLUDE,
    PHASE_SOFT_DEMOTE,
    REASON_BOOK_MISSING,
    REASON_QUOTE_STALE,
    REASON_SPREAD_TOO_WIDE,
    REASON_VOLUME_TOO_LOW,
    CandidateQuality,
    CandidateQualityError,
    candidate_quality_key,
    filter_candidates,
    resolve_candidate_quality,
    validate_chain,
)
from futu_quant.data_quality import NEW_YORK
from futu_quant.earnings_gamma import screen_gamma_contracts
from futu_quant.store import TradingStore


def _fixture_contract() -> tuple[str, str]:
    """测试用合约的到期日必须落在 campaign 的 [maturity_min_dte, maturity_max_dte] 内。

    写死 `2026-10-30` 会在日期推进后掉到 4 DTE < min 5，用例就因「未满最小到期天数」
    提前返回而恒红（2026-10-26 实测），属于夹具缺陷而非实现缺陷。
    """
    expiry = datetime.now(NEW_YORK).date() + timedelta(days=10)
    return expiry.isoformat(), f"US.META{expiry:%y%m%d}P660000"


class FakeStore:
    def __init__(self, settings: dict | None = None):
        self.settings = dict(settings or {})
        self.rows: list[dict] = []

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def record_candidate_exclusions(self, records):
        self.rows.extend(records)
        return len(records)


# --------------------------------------------------------------------------
# 注入点（设计 §8 验收 2）：可控 update_time 的报价
# --------------------------------------------------------------------------


@pytest.fixture
def stub_quotes_with_age():
    """返回 ``(quotes, chain)``：报价的 `update_time` 可控。

    设计指定的注入点是 patch `volatility_roll.broker.snapshots` —— 因为
    `volatility_roll.py:145` 的 `quotes = broker.snapshots(...)` 是**实时行情**，
    **真实券商构造不出「1250 秒前」的报价**。这里构造的是同一份 `quotes` 的等价物，
    喂给同一条链路（`screen_gamma_contracts` → `filter_candidates`）。
    """

    def _stub(age_seconds: float, *, spread_pct: float = 2.0, strike: float = 100.0,
              underlying: str = "US.META", volume: float = 1000.0) -> tuple[pd.DataFrame, pd.DataFrame]:
        now = datetime.now(NEW_YORK)
        updated = (now - timedelta(seconds=age_seconds)).isoformat()
        code = f"{underlying}261030C{int(strike * 1000)}"
        mid = 5.0
        half = mid * spread_pct / 200.0
        chain = pd.DataFrame([{
            "code": code, "option_type": "CALL", "strike_price": strike,
            "strike_time": "2026-10-30", "option_open_interest": 900,
            "suspension": False,
        }])
        quotes = pd.DataFrame([{
            "code": code, "bid_price": mid - half, "ask_price": mid + half,
            "update_time": updated, "volume": volume,
        }])
        return quotes, chain

    return _stub


def _candidates(quotes, chain, spot=100.0, **kwargs) -> pd.DataFrame:
    """走与 `volatility_roll.py:147` 完全相同的那一步。"""
    return screen_gamma_contracts(chain, quotes, spot, "CALL", **kwargs)


# --------------------------------------------------------------------------
# ① 默认值 = 现值（接管不得改变行为）
# --------------------------------------------------------------------------


def test_defaults_match_existing_literals():
    config = resolve_candidate_quality(FakeStore(), "futu_real_master")

    assert config.hard_oi_min == 500
    assert config.hard_spread_pct == 12
    assert config.hard_moneyness_max == 10
    assert config.entry_max_spread_pct == 12, "上界必须 = 真实入场门禁（VOLATILITY 默认 12），不能是 20"
    assert config.hard_stale_seconds == 180
    assert config.source == "default"


def test_real_tenant_defaults_to_stricter_soft_line():
    """实盘默认软线 5%、模拟 10%（设计 §3）—— 实盘严、模拟松。"""
    assert resolve_candidate_quality(FakeStore(), "t", is_real=True).soft_spread_pct == 5.0
    assert resolve_candidate_quality(FakeStore(), "t", is_real=False).soft_spread_pct == 10.0


# --------------------------------------------------------------------------
# ② 链式校验 fail-closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "soft,hard,entry",
    [(13, 12, 20), (5, 25, 20), (25, 12, 20)],
)
def test_chain_validation_rejects(soft, hard, entry):
    with pytest.raises(CandidateQualityError):
        validate_chain(CandidateQuality(
            soft_spread_pct=soft, hard_spread_pct=hard, entry_max_spread_pct=entry,
        ))


def test_chain_upper_bound_is_the_real_entry_gate():
    """链式校验的上界必须是**真实入场门禁（12）**，否则会掩盖配置不一致。

    反例：`hard_spread_pct=15` 而入场门禁是 12 —— 若上界写成 20，这条能通过校验，
    于是出现「候选层放行 15%、入场层在 12% 拦住」的**不可解释 BLOCK**。
    """
    store = FakeStore({candidate_quality_key("t"): json.dumps({
        "soft_spread_pct": 5, "hard_spread_pct": 15, "entry_max_spread_pct": 12,
    })})
    with pytest.raises(CandidateQualityError):
        resolve_candidate_quality(store, "t")

    # 默认值本身也必须是 12（= 真实门禁），不是 20
    assert resolve_candidate_quality(FakeStore(), "t").entry_max_spread_pct == 12


def test_chain_validation_accepts_equal_boundaries():
    validate_chain(CandidateQuality(
        soft_spread_pct=12, hard_spread_pct=12, entry_max_spread_pct=12,
    ))


def test_tenant_config_with_broken_chain_is_refused():
    """**关键**：只校验「软 ≤ 入场」是不够的 —— 硬筛设成 25% 会重新制造不可解释 BLOCK。"""
    store = FakeStore({candidate_quality_key("t"): json.dumps({
        "soft_spread_pct": 5, "hard_spread_pct": 25, "entry_max_spread_pct": 20,
    })})
    with pytest.raises(CandidateQualityError):
        resolve_candidate_quality(store, "t")


def test_invalid_config_does_not_silently_fall_back():
    store = FakeStore({candidate_quality_key("t"): json.dumps({"hard_oi_min": "abc"})})
    with pytest.raises(CandidateQualityError):
        resolve_candidate_quality(store, "t")


def test_broken_json_is_refused():
    store = FakeStore({candidate_quality_key("t"): "{not json"})
    with pytest.raises(CandidateQualityError):
        resolve_candidate_quality(store, "t")


def test_tenant_config_takes_effect():
    store = FakeStore({candidate_quality_key("t"): json.dumps({
        "hard_spread_pct": 8, "soft_spread_pct": 4, "entry_max_spread_pct": 20,
    })})
    config = resolve_candidate_quality(store, "t")

    assert config.hard_spread_pct == 8
    assert config.soft_spread_pct == 4
    assert config.source == candidate_quality_key("t")


# --------------------------------------------------------------------------
# ③ 报价停滞 → 硬排除（含实测值）
# --------------------------------------------------------------------------


def test_stale_quote_is_hard_excluded_with_measured_age(stub_quotes_with_age):
    """设计 §8 验收 2：停滞 >180s 不进候选池，`age_seconds` 记**实测值**。"""
    quotes, chain = stub_quotes_with_age(1250)
    candidates = _candidates(quotes, chain)
    assert not candidates.empty, "夹具前提：先通过既有硬筛"

    kept, records = filter_candidates(candidates, CandidateQuality(), tenant_id="t")

    assert kept.empty, "停滞 1250s 的报价必须被硬排除"
    assert len(records) == 1
    assert records[0]["reason"] == REASON_QUOTE_STALE
    assert records[0]["phase"] == PHASE_HARD_EXCLUDE
    assert records[0]["age_seconds"] == pytest.approx(1250, abs=5)
    assert records[0]["tenant_id"] == "t"


def test_fresh_quote_is_kept(stub_quotes_with_age):
    quotes, chain = stub_quotes_with_age(5)
    kept, records = filter_candidates(_candidates(quotes, chain), CandidateQuality(), tenant_id="t")

    assert len(kept) == 1
    assert records == []
    assert bool(kept.iloc[0]["soft_demoted"]) is False


def test_missing_update_time_is_book_missing(stub_quotes_with_age):
    quotes, chain = stub_quotes_with_age(5)
    quotes["update_time"] = ""
    kept, records = filter_candidates(_candidates(quotes, chain), CandidateQuality(), tenant_id="t")

    assert kept.empty
    assert records[0]["reason"] == REASON_BOOK_MISSING
    assert records[0]["age_seconds"] is None, "拿不到时间就没有实测秒数，不能编一个"


# --------------------------------------------------------------------------
# ④ 软降权 ≠ 硬排除
# --------------------------------------------------------------------------


def test_spread_between_soft_and_hard_is_demoted_not_excluded(stub_quotes_with_age):
    """设计 §8 验收 3：软线与硬线之间的价差**不排除，只降权**。

    实现成硬排除即失败 —— 那会把大量合约砍掉，回到「没有候选」的老问题。
    """
    config = CandidateQuality(soft_spread_pct=5, hard_spread_pct=12, entry_max_spread_pct=20)
    quotes, chain = stub_quotes_with_age(5, spread_pct=8.0)  # 8% 落在 5~12 之间
    candidates = _candidates(quotes, chain)
    assert not candidates.empty, "夹具前提：8% 应通过 12% 硬筛"

    kept, records = filter_candidates(candidates, config, tenant_id="t")

    assert len(kept) == 1, "8% 价差不得被硬排除（软降权不是硬排除）"
    assert bool(kept.iloc[0]["soft_demoted"]) is True
    assert records[0]["reason"] == REASON_SPREAD_TOO_WIDE
    assert records[0]["phase"] == PHASE_SOFT_DEMOTE
    assert records[0]["spread_pct"] == pytest.approx(8.0, abs=0.1)


def test_spread_beyond_hard_line_is_excluded(stub_quotes_with_age):
    """超过硬线（12%）才是硬排除 —— 与软降权必须分得清。"""
    config = CandidateQuality(soft_spread_pct=5, hard_spread_pct=12, entry_max_spread_pct=20)
    quotes, chain = stub_quotes_with_age(5, spread_pct=15.0)
    kept, records = filter_candidates(_candidates(quotes, chain), config, tenant_id="t")

    assert kept.empty
    assert records[0]["phase"] == PHASE_HARD_EXCLUDE
    assert records[0]["spread_pct"] == pytest.approx(15.0, abs=0.2)


def test_low_volume_is_demoted_not_excluded(stub_quotes_with_age):
    config = CandidateQuality(soft_volume_min=5000)
    quotes, chain = stub_quotes_with_age(5, volume=10)
    kept, records = filter_candidates(_candidates(quotes, chain), config, tenant_id="t")

    assert len(kept) == 1, "量能低只降权"
    assert records[0]["reason"] == REASON_VOLUME_TOO_LOW
    assert records[0]["phase"] == PHASE_SOFT_DEMOTE


def test_soft_demoted_column_always_present(stub_quotes_with_age):
    """下游要按 `soft_demoted` 做主排序键 —— 空候选时也必须有序，否则排序会炸。"""
    quotes, chain = stub_quotes_with_age(5)
    kept, _ = filter_candidates(pd.DataFrame(), CandidateQuality())
    assert "soft_demoted" in kept.columns
    assert "soft_demoted" in filter_candidates(
        _candidates(quotes, chain), CandidateQuality()
    )[0].columns


def test_exclusions_never_touch_runtime_config():
    """排除留痕**绝不写 `runtime_config`**，也绝不写无前缀全局键（设计 §4.1c）。"""
    import inspect

    from futu_quant import candidate_quality as module

    source = inspect.getsource(module)
    assert "set_setting" not in source, "过滤器不得写配置键"
    assert "runtime_config" not in source


# --------------------------------------------------------------------------
# ⑤ 表走版本化迁移（存量库也要有）
# --------------------------------------------------------------------------


def test_candidate_exclusions_table_exists_on_existing_db(tmp_path, monkeypatch):
    """存量库必须也能拿到 `candidate_exclusions`。

    今天刚栽过「迁移写在 `initialize()` 的新建库分支」—— 存量库走
    `apply_migrations()` 那条路，永远拿不到。这里用**含 `order_intents` 的旧库**构造，
    确保走的是存量库路径。
    """
    db = tmp_path / "legacy.db"
    TradingStore(db)  # 建出完整 schema

    import sqlite3

    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE candidate_exclusions")
    raw.execute("DELETE FROM schema_migrations WHERE version = 11")
    raw.commit()
    assert raw.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'"
    ).fetchone() is not None, "夹具失效：没有 order_intents，会走新建库分支"
    raw.close()

    # 存量库升级必须显式授权（与 run_service 启动路径同口径），未授权时 __init__ 会拒绝
    from futu_quant.migrations import authorize_migrations
    authorize_migrations("test:candidate_quality_filter")
    store = TradingStore(db)
    with store.connection() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidate_exclusions'"
        ).fetchone()
    assert exists is not None, "存量库没拿到 candidate_exclusions —— 迁移路径不对"


def test_exclusion_roundtrip_and_prune(tmp_path):
    store = TradingStore(tmp_path / "ce.db")
    written = store.record_candidate_exclusions([
        {"tenant_id": "t1", "strategy_id": "volatility_long_gamma", "underlying": "US.META",
         "code": "US.META261030C100000", "reason": REASON_QUOTE_STALE,
         "age_seconds": 1250.0, "spread_pct": 2.0, "volume": 10.0,
         "phase": PHASE_HARD_EXCLUDE},
        {"tenant_id": "t1", "strategy_id": "volatility_long_gamma", "underlying": "US.META",
         "code": "US.META261030C105000", "reason": REASON_SPREAD_TOO_WIDE,
         "age_seconds": 5.0, "spread_pct": 8.0, "volume": 100.0,
         "phase": PHASE_SOFT_DEMOTE},
    ])
    assert written == 2

    summary = {row["reason"]: row["count"] for row in store.candidate_exclusion_summary("t1")}
    assert summary[REASON_QUOTE_STALE] == 1
    assert summary[REASON_SPREAD_TOO_WIDE] == 1

    # 保留策略：7 天前的行必须能被清掉
    with store.connection() as connection:
        connection.execute(
            "UPDATE candidate_exclusions SET created_at = '2020-01-01T00:00:00+00:00'"
        )
    assert store.prune_candidate_exclusions(days=7) == 2
    assert store.candidate_exclusion_summary("t1") == []


# --------------------------------------------------------------------------
# ⑥ 入场层：降级上限 900 → 300，且与候选层同源
# --------------------------------------------------------------------------


def _quiet_but_usable_row(age_seconds: float) -> dict:
    return {
        "bid_price": 4.9, "ask_price": 5.1,
        "update_time": (datetime.now(NEW_YORK) - timedelta(seconds=age_seconds)).isoformat(),
    }


def test_entry_gate_default_cap_is_300_not_900():
    """默认降级上限 = **300s**（P0-5 §5.1）。400s 的报价在旧口径下会"降级通过"。"""
    from futu_quant.data_quality import option_quote_freshness

    allowed, reason, age, degraded = option_quote_freshness(_quiet_but_usable_row(400))

    assert age == pytest.approx(400, abs=5)
    assert allowed is False, "400s 超过 300s 上限，必须拦住（旧 900s 口径会放行）"
    assert degraded is False


def test_entry_gate_cap_is_configurable():
    """配置可覆盖：显式传 900 时，400s 仍走降级通过 —— 证明上限是可配的、不是写死。"""
    from futu_quant.data_quality import option_quote_freshness

    allowed, _reason, _age, degraded = option_quote_freshness(
        _quiet_but_usable_row(400), maximum_seconds=900,
    )

    assert allowed is True and degraded is True


def test_entry_cap_comes_from_same_config_source():
    """入场层与候选层读**同一个**解析器（设计 §3 硬规则 2）。"""
    store = FakeStore({candidate_quality_key("t"): json.dumps({"entry_max_stale_seconds": 600})})
    assert resolve_candidate_quality(store, "t").entry_max_stale_seconds == 600
    assert resolve_candidate_quality(FakeStore(), "t").entry_max_stale_seconds == 300


def test_quote_age_percentiles_are_queryable(tmp_path):
    """§8 验收 11：`quote_age_at_submit` 可查，P50/P95 可出。"""
    store = TradingStore(tmp_path / "pct.db")
    store.record_candidate_exclusions([
        {"tenant_id": "t1", "strategy_id": "s", "underlying": "U", "code": f"C{i}",
         "reason": "ADMITTED", "age_seconds": float(age), "spread_pct": 1.0,
         "volume": None, "phase": "ADMITTED"}
        for i, age in enumerate([10, 20, 30, 40, 50, 60, 70, 80, 90, 300])
    ])

    stats = store.candidate_quote_age_percentiles("t1")

    assert stats["count"] == 10
    assert stats["p50"] == pytest.approx(50, abs=1)
    assert stats["p95"] is not None and stats["p95"] >= stats["p50"]


# --------------------------------------------------------------------------
# ⑦ A0 不变量：过滤到空 → 保留原 campaign（退出管理不受影响）
# --------------------------------------------------------------------------


def test_filter_to_empty_returns_original_campaign(monkeypatch):
    """A0（设计 §2.5，Reviewer 阻断项）：**过滤到空必须原样返回原 campaign**。

    这是 META 那次的失效模式 —— 把「被排除」实现成「跳过 symbol」，
    已持仓仓位会同时失去止盈/止损/平仓。判据：返回的 campaign **一字未改**。
    """
    from futu_quant import volatility_roll as vr

    campaign = {
        "underlying": "US.META", "code": _fixture_contract()[1],
        "tenant_id": "futu_real_master", "maturity_min_dte": 5, "maturity_max_dte": 21,
    }
    quote_row = {"code": "US.META", "last_price": 100.0,
                 "update_time": datetime.now(NEW_YORK).isoformat()}

    class Broker:
        def snapshots(self, codes):
            return pd.DataFrame([quote_row])

        def candles(self, code, count=120):
            return pd.DataFrame({"close": [1.0] * 30})

        def option_expirations(self, underlying):
            return pd.DataFrame([{"strike_time": _fixture_contract()[0]}])

        def option_chain(self, underlying, expiry):
            return pd.DataFrame([{
                "code": _fixture_contract()[1], "option_type": "PUT",
                "strike_price": 66.0, "option_open_interest": 900, "suspension": False,
            }])

    class Store:
        def option_history_for_underlying(self, underlying):
            return []

        def record_candidate_exclusions(self, records):
            return len(records)

    monkeypatch.setattr(vr, "volatility_threshold_profile",
                        lambda bars: {"entry_zscore": 2.0, "minimum_underlying_move": 0.02})
    monkeypatch.setattr(vr, "volatility_regime",
                        lambda *a, **k: {"active": True, "direction_score": 3, "reason": "t"})
    monkeypatch.setattr(vr, "automatic_option_direction",
                        lambda *a, **k: ("PUT", "t", 1))
    monkeypatch.setattr(vr, "screen_gamma_contracts",
                        lambda *a, **k: pd.DataFrame([{"code": "US.META261030P660000"}]))
    # 模拟「全部候选都被过滤掉」
    monkeypatch.setattr(vr, "filter_candidates",
                        lambda candidates, config, **k: (pd.DataFrame(), [{"code": "x"}]))
    monkeypatch.setattr(vr, "realized_volatility", lambda bars: 0.5)

    returned, message = vr.roll_campaign(Broker(), dict(campaign), Store())

    assert returned["code"] == campaign["code"], (
        "过滤到空把原 campaign 改掉了 —— 已持仓会失去退出管理（META 失效模式）"
    )
    assert returned.get("status") != "EXPIRED"
    assert "候选质量过滤后没有可评分的新合约" in message


def test_exclusion_record_failure_is_visible_and_does_not_block_selection(monkeypatch):
    """留痕写失败**必须可见**（ERROR 事件），且**不得阻断选标的**。

    裸 `except: pass` 会让「候选质量页显示 0 条排除、而过滤照旧在排除」——
    **那正是本需求要消灭的沉默过滤器**。选标的当然不该被阻断，但**必须可见**。
    """
    from futu_quant import volatility_roll as vr

    events: list[dict] = []

    class Store:
        def option_history_for_underlying(self, underlying):
            return []

        def record_candidate_exclusions(self, records):
            raise RuntimeError("库写失败演练")

        def operational_event(self, component, event_type, status, payload=None):
            events.append({
                "component": component, "event_type": event_type,
                "status": status, "payload": payload or {},
            })

        def operational_events(self, limit=500):
            return list(reversed(events))[:limit]

    class Broker:
        def snapshots(self, codes):
            return pd.DataFrame([{
                "code": "US.META", "last_price": 100.0,
                "update_time": datetime.now(NEW_YORK).isoformat(),
            }])

        def candles(self, code, count=120):
            return pd.DataFrame({"close": [1.0] * 30})

        def option_expirations(self, underlying):
            return pd.DataFrame([{"strike_time": _fixture_contract()[0]}])

        def option_chain(self, underlying, expiry):
            return pd.DataFrame([{
                "code": _fixture_contract()[1], "option_type": "PUT",
                "strike_price": 66.0, "option_open_interest": 900, "suspension": False,
            }])

    campaign = {
        "underlying": "US.META", "code": _fixture_contract()[1],
        "tenant_id": "futu_real_master", "maturity_min_dte": 5, "maturity_max_dte": 21,
    }

    def _passthrough(frame, *args, **kwargs):
        return frame

    def _rank_contracts(frame, *args, **kwargs):
        result = frame.copy()
        result["volatility_eligible"] = False
        result["volatility_filter_reason"] = "演练"
        return result

    monkeypatch.setattr(vr, "volatility_threshold_profile",
                        lambda bars: {"entry_zscore": 2.0, "minimum_underlying_move": 0.02})
    monkeypatch.setattr(vr, "volatility_regime",
                        lambda *a, **k: {"active": True, "direction_score": 3, "reason": "t"})
    monkeypatch.setattr(vr, "automatic_option_direction", lambda *a, **k: ("PUT", "t", 1))
    monkeypatch.setattr(vr, "screen_gamma_contracts",
                        lambda *a, **k: pd.DataFrame([{"code": "US.META261030P660000"}]))
    # 过滤**排除了 1 个**候选，但候选池**非空** → 必须继续往下走
    monkeypatch.setattr(vr, "filter_candidates", lambda candidates, config, **k: (
        pd.DataFrame([{"code": "US.META261030P660000"}]), [{"code": "US.META261030P660000"}],
    ))
    monkeypatch.setattr(vr, "realized_volatility", lambda bars: 0.5)
    monkeypatch.setattr(vr, "rank_long_option_candidates", _passthrough)
    monkeypatch.setattr(vr, "add_iv_history_percentiles", _passthrough)
    monkeypatch.setattr(vr, "tiered_premium_budget", lambda *a, **k: (1000.0, "tier"))
    monkeypatch.setattr(vr, "rank_volatility_contracts", _rank_contracts)
    monkeypatch.setattr(vr, "volatility_min_expected_return", lambda *a, **k: 0.0)
    # 结构构建阶段没有合格结构 → 走到「没有合约同时通过…」那条正常返回，
    # 以此证明**留痕失败之后函数仍继续往下走**（而不是被阻断在过滤那一步）。
    monkeypatch.setattr(
        vr, "build_volatility_structure_candidates",
        lambda frame, direction, **k: pd.DataFrame({"volatility_eligible": [False]}),
    )

    returned, message = vr.roll_campaign(Broker(), dict(campaign), Store())

    # ① 必须可见
    assert events, "留痕失败被静默吞掉了 —— 过滤器又变回静默"
    assert events[0]["event_type"] == "CANDIDATE_EXCLUSION_RECORD_FAILED"
    assert events[0]["status"] == "ERROR"
    assert events[0]["payload"]["excluded_count"] == 1
    assert events[0]["payload"]["underlying"] == "US.META"

    # ② 不得阻断选标的：走完了过滤之后的下游，而不是"过滤到空"那条早退
    assert "候选质量过滤后没有可评分的新合约" not in message, (
        "留痕失败把选标的也阻断了 —— 应该只留痕、不阻断"
    )
