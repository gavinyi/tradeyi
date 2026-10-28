"""波动率入场 IV 闸门：fail-closed + 阈值单一来源 + 实时值观测。

对应 2026-10-29 批准的 5 条改动中的 1/2/5：
1. `rank_volatility_contracts` 原先「IV 百分位缺失 → 放行」（全仓唯一 fail-open 门槛）
   → 改为 fail-closed 拦下；
2. 同一道闸门两个真值源（`volatility_structures` 用 `resolve_iv_percentile_cap`、
   `volatility_optimizer` 写死 `.75`）→ 合并到同一解析器；
3. 原因文案必须区分「字段缺失（证据不足）」与「已算出但超限」，
   不许再把缺失伪装成「100% 超过 75%」；
5. 落 `VOLATILITY_IV_GATE` 审计，记录**当前候选实时** IV 百分位与生效阈值
   （此前照录的是 campaign 选定时存的旧值，导致 `:87` 永远黑箱）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import pytest

from futu_quant.store import TradingStore
from futu_quant.volatility_optimizer import rank_volatility_contracts
from futu_quant.volatility_structures import DEFAULT_IV_CAP, resolve_iv_percentile_cap


def _candidate(**overrides) -> pd.DataFrame:
    expiry = (date.today() + timedelta(days=9)).isoformat()
    row = {
        "code": "US.NVDA261106P215000",
        "strike_time": f"{expiry} 00:00:00",
        "option_type": "PUT",
        "strike_price": 215.0,
        "option_delta": -0.50,
        "spread_pct": 1.0,
        "option_open_interest": 5000,
        "volume": 1000,
        "option_implied_volatility": 45.0,
        "iv_history_percentile": 0.50,
        "iv_percentile_status": "HISTORICAL",
        "model_expected_return": 0.20,
        "option_contract_multiplier": 100.0,
        "ask_price": 5.0,
        "mid_price": 5.0,
        "bid_price": 4.95,
        "option_gamma": 0.02,
        "option_theta": -0.05,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _rank(cand: pd.DataFrame, store=None, campaign=None):
    return rank_volatility_contracts(
        cand, spot=200.0, rv=0.30, iv_percentile=0.50, as_of=date.today(),
        signal_active=True, strong_signal=False,
        max_premium_per_stage=6000.0, contracts_per_stage=1,
        store=store, campaign=campaign,
    )


def test_missing_iv_percentile_is_blocked_not_passed():
    """契约 1：IV 百分位缺失必须**拦**（旧实现是放行）。"""
    ranked = _rank(_candidate(iv_history_percentile=None))
    row = ranked.iloc[0]
    assert bool(row["volatility_eligible"]) is False, "缺失被放行了 —— fail-open 未修"
    assert "缺失" in str(row["volatility_filter_reason"])
    assert "证据不足" in str(row["volatility_filter_reason"])


def test_missing_reason_is_not_disguised_as_overflow():
    """契约 2：缺失的原因不能写成「超过 X%」。"""
    reason = str(_rank(_candidate(iv_history_percentile=None)).iloc[0]["volatility_filter_reason"])
    assert "超过" not in reason.split("；")[0] or "缺失" in reason
    assert "缺失" in reason


def test_over_limit_reason_still_reports_overflow():
    """契约 3：真超限时原因仍写「超过」，且阈值来自解析器（默认 75%）。"""
    ranked = _rank(_candidate(iv_history_percentile=0.97))
    reason = str(ranked.iloc[0]["volatility_filter_reason"])
    assert "超过 75%" in reason
    assert "缺失" not in reason


def test_campaign_iv_cap_is_honoured_by_optimizer_gate():
    """契约 4：optimizer 侧必须走同一解析器 —— campaign 放宽到 0.95 后 0.90 应放行。"""
    ranked = _rank(
        _candidate(iv_history_percentile=0.90),
        campaign={"maximum_entry_iv_percentile": 0.95},
    )
    assert bool(ranked.iloc[0]["volatility_eligible"]) is True

    # 不配 campaign 时默认 0.75，同样输入必须被拦
    ranked_default = _rank(_candidate(iv_history_percentile=0.90))
    assert bool(ranked_default.iloc[0]["volatility_eligible"]) is False


def test_resolver_is_single_source_of_truth():
    assert resolve_iv_percentile_cap(None) == DEFAULT_IV_CAP == 0.75
    assert resolve_iv_percentile_cap({"maximum_entry_iv_percentile": 0.90}) == 0.90


def test_audit_records_live_percentile_not_campaign_snapshot(tmp_path):
    """契约 5：审计必须记录**当前候选实时** IV 百分位，而不是 campaign 旧值。

    生产实例：campaign 里存的是选定时 0.7382，而此刻候选是 0.97。
    若审计只写 0.7382，`volatility_structures.py` 的闸门永远黑箱。
    """
    store = TradingStore(tmp_path / "t.db")
    campaign = {
        "tenant_id": "futu_paper_sandbox",
        "strategy_id": "volatility_long_gamma",
        "iv_history_percentile": 0.7382,   # campaign 旧快照
        "maximum_entry_iv_percentile": 0.75,
    }
    _rank(_candidate(iv_history_percentile=0.97), store=store, campaign=campaign)

    rows = [r for r in store.audit_events() if r["event_type"] == "VOLATILITY_IV_GATE"]
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["iv_percentile_live"] == pytest.approx(0.97), (
        "审计记录的不是实时值 —— 会继续把 campaign 旧值当成当前值"
    )
    assert payload["iv_cap_effective"] == pytest.approx(0.75)
    assert payload["passed"] is False
    assert payload["code"] == "US.NVDA261106P215000"
    assert payload["tenant_id"] == "futu_paper_sandbox"


def test_audit_marks_missing_as_null_not_zero(tmp_path):
    """契约 6：缺失时实时值必须是 None，不能写 0 假装「百分位为 0」。"""
    store = TradingStore(tmp_path / "t.db")
    _rank(_candidate(iv_history_percentile=None), store=store)
    payload = json.loads(
        [r for r in store.audit_events() if r["event_type"] == "VOLATILITY_IV_GATE"][0]["payload_json"]
    )
    assert payload["iv_percentile_live"] is None
    assert payload["passed"] is False


def test_audit_only_records_blocked_candidates(tmp_path):
    """契约 7（Reviewer 条件）：放行的候选不写 `VOLATILITY_IV_GATE`。

    审计在逐候选循环内，若放行也写会日增数千条、压垮 audit 表；
    放行的候选已有 `VOLATILITY_ENTRY_QUALITY` 覆盖，不重复。
    """
    store = TradingStore(tmp_path / "t.db")
    # 必过：IV 百分位 0.50 <= 0.75 → 不该写审计
    _rank(_candidate(iv_history_percentile=0.50), store=store)
    assert [r for r in store.audit_events()
            if r["event_type"] == "VOLATILITY_IV_GATE"] == []

    # 被拦：0.97 > 0.75 → 必须写
    _rank(_candidate(iv_history_percentile=0.97), store=store)
    assert len([r for r in store.audit_events()
                if r["event_type"] == "VOLATILITY_IV_GATE"]) == 1

