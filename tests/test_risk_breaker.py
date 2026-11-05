"""G4 回撤熔断纯判定内核守卫（`src/futu_quant/risk_breaker.py`）。

对齐设计基线 `c227416` 的 V1–V22 中纯逻辑部分：
- 双基准各自只涨不跌，回撤恒 >= 0（Reviewer C1 / V2）；
- peak 初始化必须 backfill，禁止用当前权益冒充（假绿守卫 / V1）；
- 阈值类缺键或非法 → fail-closed（V5）；
- 影响判定：1 档滞回、2 档不自动降级、ack 双向清除、预算减半恢复（V6–V9, V11–V14）；
- 三件套原子解除，缺一件无效且 30 天 ≤1 次（V12 / V13）；
- shadow 与 enforce 共用同一判定，只有是否真拦不同（V10）；
- 原因聚合只汇总、不改判定，且 SELL 永不走这里（V16 / V23）。
"""

from __future__ import annotations

import re

import pytest

from futu_quant import risk_breaker as rb

BASE_KW = dict(peak_hist=100_000.0, peak_reset=100_000.0, baseline_equity=100_000.0)
TH = rb.BreakerThresholds(warn=0.08, stop=0.10, resume=0.06, rearm_gap_pp=0.02, mode=rb.MODE_ENFORCE)
TH_SHADOW = rb.BreakerThresholds(warn=0.08, stop=0.10, resume=0.06, rearm_gap_pp=0.02,
                                 mode=rb.MODE_SHADOW)


def _state(**overrides) -> rb.RiskState:
    base = dict(
        tenant_id="2817",
        peak_hist=100_000.0,
        peak_hist_at="2026-10-20T14:00:00",
        baseline_equity=100_000.0,
        baseline_at="2026-10-20T14:00:00",
        peak_reset=100_000.0,
        updated_at="2026-10-20T14:00:00",
    )
    base.update(overrides)
    return rb.RiskState(**base)


# ------------------------------------------------------------------ 回撤口径
def test_drawdowns_never_negative_and_peaks_monotonic():
    dd = rb.compute_drawdowns(120_000.0, **BASE_KW)
    assert dd.dd_vs_peak == 0.0 and dd.dd_since_reset == 0.0
    assert dd.peak_hist == 120_000.0 and dd.peak_reset == 120_000.0
    # 回落不降峰值
    dd2 = rb.compute_drawdowns(90_000.0, peak_hist=dd.peak_hist, peak_reset=dd.peak_reset,
                              baseline_equity=100_000.0)
    assert dd2.dd_vs_peak == pytest.approx(0.25)
    assert dd2.peak_hist == 120_000.0


def test_reset_baseline_does_not_hide_historical_drawdown():
    """重置基准 dd_since_reset=0，但 dd_vs_peak 仍揭示深水区（双基准不可互相洗白）。"""
    dd = rb.compute_drawdowns(
        90_500.0, peak_hist=100_000.0, peak_reset=90_500.0, baseline_equity=90_500.0
    )
    assert dd.dd_since_reset == 0.0
    assert dd.dd_vs_peak == pytest.approx(0.095)


def test_initial_peaks_requires_backfill_and_takes_max():
    with pytest.raises(ValueError, match="backfill"):
        rb.initial_peaks(current_equity=90_500.0, historical_high=None)
    peak_hist, peak_reset = rb.initial_peaks(current_equity=90_500.0, historical_high=100_000.0)
    assert peak_hist == 100_000.0
    # 重置基准起点 = baseline = 当前权益（深水区不会被当成 0 回撤）
    assert peak_reset == 90_500.0
    # 历史峰值低于当前权益时取当前权益（不得低于权益）
    peak_hist2, _ = rb.initial_peaks(current_equity=90_500.0, historical_high=80_000.0)
    assert peak_hist2 == 90_500.0


def test_equity_sample_rejects_bad_and_outlier():
    assert rb.validate_equity_sample(None, 100.0).ok is False
    assert rb.validate_equity_sample(0, 100.0).ok is False
    assert rb.validate_equity_sample(-5, 100.0).ok is False
    assert rb.validate_equity_sample(float("nan"), 100.0).ok is False
    # 单轮跳变 >40% 视为异常
    assert rb.validate_equity_sample(150.0, 100.0).ok is False
    assert rb.validate_equity_sample(110.0, 100.0).ok is True


# ------------------------------------------------------------------ 配置 fail-closed
def _getter(mapping: dict[str, str]):
    return lambda key: mapping.get(key)


def test_thresholds_missing_any_key_fails_closed():
    full = {
        "risk_drawdown_warn_pct:2817": "0.08",
        "risk_drawdown_stop_pct:2817": "0.10",
        "risk_drawdown_resume_pct:2817": "0.06",
        "risk_drawdown_rearm_gap_pp:2817": "0.02",
        "risk_breaker_mode:2817": "shadow",
    }
    loaded = rb.load_thresholds(_getter(full), "2817")
    assert loaded.mode == rb.MODE_SHADOW
    for missing_key in full:
        partial = dict(full)
        partial.pop(missing_key)
        if missing_key == "risk_breaker_mode:2817":
            # mode 非阈值类：缺省 = off（不拒绝）
            assert rb.load_thresholds(_getter(partial), "2817").mode == rb.MODE_OFF
        else:
            with pytest.raises(rb.BreakerConfigError, match="缺键"):
                rb.load_thresholds(_getter(partial), "2817")


def test_thresholds_invalid_chain_and_values_fail_closed():
    bad_chain = {
        "risk_drawdown_warn_pct:1": "0.10",
        "risk_drawdown_stop_pct:1": "0.08",
        "risk_drawdown_resume_pct:1": "0.06",
        "risk_drawdown_rearm_gap_pp:1": "0.02",
    }
    with pytest.raises(rb.BreakerConfigError, match="阈值链"):
        rb.load_thresholds(_getter(bad_chain), "1")
    non_numeric = {
        "risk_drawdown_warn_pct:1": "abc",
        "risk_drawdown_stop_pct:1": "0.10",
        "risk_drawdown_resume_pct:1": "0.06",
        "risk_drawdown_rearm_gap_pp:1": "0.02",
    }
    with pytest.raises(rb.BreakerConfigError, match="不是数字"):
        rb.load_thresholds(_getter(non_numeric), "1")
    bad_mode = dict(bad_chain, **{
        "risk_drawdown_warn_pct:1": "0.08",
        "risk_breaker_mode:1": "live",
    })
    with pytest.raises(rb.BreakerConfigError, match="非法"):
        rb.load_thresholds(_getter(bad_mode), "1")


# ------------------------------------------------------------------ 状态机
def test_warn_level_blocks_in_enforce_and_shadow_records_would_block():
    sample = rb.validate_equity_sample(91_000.0, None)
    enforce = rb.evaluate(_state(), sample, TH, now="2026-10-25T14:00:00",
                         current_trading_day="2026-10-25")
    shadow = rb.evaluate(_state(), sample, TH_SHADOW, now="2026-10-25T14:00:00",
                         current_trading_day="2026-10-25")
    # 同一份判定：等级相同、原因相同，只有是否真拦不同
    assert enforce.level == shadow.level == 1
    assert enforce.would_block is shadow.would_block is True
    assert enforce.blocked is True and shadow.blocked is False
    assert enforce.reasons == shadow.reasons
    assert enforce.reasons[0].code == rb.DRAWDOWN_WARN
    assert enforce.reasons[0].as_audit()["blocked_by"] == rb.DRAWDOWN_WARN
    assert any(level == "WARNING" for level, _ in enforce.alerts)


def test_resume_hysteresis_and_two_levels_never_auto_downgrade():
    # 处于 1 档、回撤回到 7%（介于 resume 6% 与 warn 8%）→ 保持 1 档
    state = _state(breaker_level=1, breaker_reason="1:dd_since_reset_warn")
    decision = rb.evaluate(state, rb.validate_equity_sample(93_000.0, None), TH,
                           now="2026-10-25T14:00:00", current_trading_day="2026-10-25")
    assert decision.level == 1
    # 回到 6% 及以下 → 自动解除
    decision = rb.evaluate(state, rb.validate_equity_sample(94_000.0, None), TH,
                           now="2026-10-25T14:00:00", current_trading_day="2026-10-25")
    assert decision.level == 0
    # 2 档即使回撤归零也不自动解除
    stop_state = _state(breaker_level=2, breaker_reason="2:dd_vs_peak_absolute")
    decision = rb.evaluate(stop_state, rb.validate_equity_sample(100_000.0, None), TH,
                           now="2026-10-25T14:00:00", current_trading_day="2026-10-25")
    assert decision.level == 2
    assert decision.blocked is True


def test_absolute_floor_is_relaxed_by_ack_then_rearmed():
    # 无 ack：dd_vs_peak 11% → 2 档绝对底线
    state = _state(peak_hist=100_000.0, peak_reset=89_000.0, baseline_equity=89_000.0,
                   breaker_level=0)
    sample = rb.validate_equity_sample(89_000.0, None)
    assert rb.evaluate(state, sample, TH, now="2026-10-25T14:00:00",
                       current_trading_day="2026-10-25").level == 2
    # 有 ack=10%：底线放宽到 12%，11% 不再触发
    acked = _state(peak_hist=100_000.0, peak_reset=89_000.0, baseline_equity=89_000.0,
                   ack_dd_vs_peak=0.10, ack_at="2026-10-24T14:00:00")
    decision = rb.evaluate(acked, sample, TH, now="2026-10-24T15:00:00",
                           current_trading_day="2026-10-24")
    assert decision.level == 0
    # 再下穿 ack+2pp → 重新武装
    worse = acked
    worse = _state(peak_hist=100_000.0, peak_reset=89_000.0, baseline_equity=89_000.0,
                   ack_dd_vs_peak=0.10, ack_at="2026-10-24T14:00:00")
    decision = rb.evaluate(worse, rb.validate_equity_sample(87_000.0, None), TH,
                           now="2026-10-24T15:00:00", current_trading_day="2026-10-24")
    assert decision.level == 2
    # 锁存后绝对底线放宽到 ack+2pp = 12% → code 跟随生效底线（不是写死 10pct）
    assert decision.reasons[0].code == rb.DRAWDOWN_ABSOLUTE


def test_ack_clears_only_on_later_trading_day_when_below_stop():
    acked = _state(peak_hist=100_000.0, peak_reset=89_000.0, baseline_equity=89_000.0,
                   ack_dd_vs_peak=0.095, ack_at="2026-10-24T14:00:00",
                   breaker_level=2, de_risk_scale=0.5)
    # 同一交易日：不清除
    same_day = rb.evaluate(acked, rb.validate_equity_sample(91_000.0, None), TH,
                           now="2026-10-24T18:00:00", current_trading_day="2026-10-24")
    assert same_day.state.ack_dd_vs_peak == pytest.approx(0.095)
    # 下一交易日且 dd_vs_peak < 10% → 清除（否则 10% 底线被永久放宽）
    next_day = rb.evaluate(acked, rb.validate_equity_sample(91_000.0, None), TH,
                           now="2026-10-27T14:00:00", current_trading_day="2026-10-27")
    assert next_day.state.ack_dd_vs_peak is None
    assert next_day.state.ack_at is None


def test_de_risk_scale_restores_when_back_below_warn():
    state = _state(de_risk_scale=0.5, breaker_level=0)
    decision = rb.evaluate(state, rb.validate_equity_sample(94_000.0, None), TH,
                           now="2026-10-25T14:00:00", current_trading_day="2026-10-25")
    assert decision.state.de_risk_scale == 1.0


def test_invalid_sample_does_not_touch_state_or_judge():
    state = _state(breaker_level=1)
    decision = rb.evaluate(state, rb.EquitySample(ok=False, equity=None, reason="券商查询失败"),
                           TH, now="2026-10-25T14:00:00")
    assert decision.state is state
    assert decision.level == 1  # 保持原等级，不因异常采样升级/解除
    assert decision.blocked is False
    assert "券商查询失败" in decision.skipped_reason


def test_mode_off_never_blocks():
    off = rb.BreakerThresholds(warn=0.08, stop=0.10, resume=0.06, rearm_gap_pp=0.02, mode="off")
    decision = rb.evaluate(_state(), rb.validate_equity_sample(80_000.0, None), off,
                           now="2026-10-25T14:00:00", current_trading_day="2026-10-25")
    assert decision.blocked is False and decision.would_block is False
    assert decision.state.breaker_level == 0


# ------------------------------------------------------------------ 人工解除三件套
def test_manual_reset_requires_auth_and_sets_three_pieces_atomically():
    state = _state(breaker_level=2, breaker_reason="2:dd_since_reset_stop")
    with pytest.raises(rb.ManualResetError, match="admin"):
        rb.manual_reset(state, current_equity=89_000.0, dd_vs_peak=0.11,
                        now="2026-10-25T15:00:00", authorized=False)
    new_state = rb.manual_reset(state, current_equity=89_000.0, dd_vs_peak=0.11,
                                now="2026-10-25T15:00:00", authorized=True)
    # ① baseline 归零（起点 = 当前权益）
    assert new_state.baseline_equity == pytest.approx(89_000.0)
    assert new_state.peak_reset == pytest.approx(89_000.0)
    # ② 锁存
    assert new_state.ack_dd_vs_peak == pytest.approx(0.11)
    # ③ 预算减半
    assert new_state.de_risk_scale == pytest.approx(0.5)
    assert new_state.breaker_level == 0
    assert new_state.reset_count_30d == 1
    # 解除后立即评估：dd_since_reset=0 且 ack 放宽底线 → 不应再被拦
    decision = rb.evaluate(new_state, rb.validate_equity_sample(89_000.0, None), TH,
                           now="2026-10-25T15:05:00", current_trading_day="2026-10-25")
    assert decision.level == 0


def test_manual_reset_rejected_inside_window_with_retry_after():
    """窗口内第二次必须拒绝，并回传 `retry_after`（页面要显示「下次可解除」）。"""
    state = _state(breaker_level=2, reset_count_30d=1,
                   last_reset_at="2026-10-01T10:00:00+00:00")
    with pytest.raises(rb.ManualResetError) as excinfo:
        rb.manual_reset(state, current_equity=89_000.0, dd_vs_peak=0.11,
                        now="2026-10-20T10:00:00+00:00", authorized=True)
    assert excinfo.value.retry_after == "2026-10-31T10:00:00+00:00"


def test_manual_reset_allowed_after_window_and_counter_back_to_one():
    """窗口外必须允许（31 天后），且计数回到 1 —— 防「终身只能解除一次」。"""
    state = _state(breaker_level=2, reset_count_30d=1,
                   last_reset_at="2026-10-01T10:00:00+00:00")
    new_state = rb.manual_reset(state, current_equity=89_000.0, dd_vs_peak=0.11,
                                now="2026-11-01T11:00:00+00:00", authorized=True)
    assert new_state.reset_count_30d == 1
    assert new_state.last_reset_at == "2026-11-01T11:00:00+00:00"
    # 再隔 31 天又能解除（滑动窗口，非终身一次）
    again = rb.manual_reset(new_state, current_equity=88_000.0, dd_vs_peak=0.12,
                            now="2026-12-03T11:00:00+00:00", authorized=True)
    assert again.reset_count_30d == 1


def test_unparsable_last_reset_is_fail_closed(caplog):
    """Reviewer 小项 1：`last_reset_at` 有值但解析失败 → 视为窗口未过、拒绝 + WARN。"""
    import logging

    state = _state(breaker_level=2, reset_count_30d=1,
                   last_reset_at="not-a-timestamp")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(rb.ManualResetError, match="无法解析"):
            rb.manual_reset(state, current_equity=89_000.0, dd_vs_peak=0.11,
                            now="2026-10-25T15:00:00+00:00", authorized=True)
    assert any("无法解析" in record.message for record in caplog.records)
    assert rb.unparsable_last_reset(state) is True
    assert rb.unparsable_last_reset(_state()) is False


#: code 枚举白名单（新增/改名必须先改白名单，否则审计与文档会长期漂移）
ALLOWED_REASON_CODES = {
    "dd_since_reset_warn",
    "dd_since_reset_stop",
    "dd_vs_peak_absolute",
}


def test_tier_codes_are_semantic_and_enumerated():
    """Chief 定稿：code 用语义名、数值放 detail；白名单固定 code 集合。"""
    assert rb.DRAWDOWN_WARN == "dd_since_reset_warn"
    assert rb.DRAWDOWN_STOP == "dd_since_reset_stop"
    assert rb.DRAWDOWN_ABSOLUTE == "dd_vs_peak_absolute"
    assert {rb.DRAWDOWN_WARN, rb.DRAWDOWN_STOP, rb.DRAWDOWN_ABSOLUTE} == ALLOWED_REASON_CODES
    assert rb._level_for(0.08, 0.0, TH, None) == (1, "dd_since_reset_warn")
    assert rb._level_for(0.10, 0.0, TH, None)[1] == "dd_since_reset_stop"
    assert rb._level_for(0.0, 0.10, TH, None)[1] == "dd_vs_peak_absolute"
    # 阈值改成 7%/9%：code 仍是语义名（不带数字），具体数值由 detail 承载
    relaxed = rb.BreakerThresholds(warn=0.07, stop=0.09, resume=0.05, rearm_gap_pp=0.02,
                                   mode=rb.MODE_ENFORCE)
    assert rb._level_for(0.07, 0.0, relaxed, None)[1] == "dd_since_reset_warn"
    assert rb._level_for(0.09, 0.0, relaxed, None)[1] == "dd_since_reset_stop"
    decision = rb.evaluate(
        _state(), rb.validate_equity_sample(92_000.0, None), relaxed,
        now="2026-10-25T14:00:00", current_trading_day="2026-10-25",
    )
    assert decision.reasons[0].code == "dd_since_reset_warn"
    assert "dd_since_reset=8.00%" in decision.reasons[0].detail
    # 所有运行期产出的 code 必须落在白名单内
    emitted = {decision.reasons[0].code}
    for sample, th in ((92_000.0, TH), (90_000.0, TH), (89_000.0, TH)):
        reported = rb.evaluate(
            _state(peak_hist=100_000.0, peak_reset=89_000.0, baseline_equity=89_000.0),
            rb.validate_equity_sample(sample, None), th,
            now="2026-10-25T14:00:00", current_trading_day="2026-10-25",
        )
        emitted.update(reason.code for reason in reported.reasons)
    assert emitted <= ALLOWED_REASON_CODES, f"出现白名单外的 code：{emitted - ALLOWED_REASON_CODES}"


def test_reset_retry_after_none_when_window_expired_or_never_reset():
    assert rb.reset_retry_after(_state(), now="2026-11-01T00:00:00+00:00") is None
    expired = _state(last_reset_at="2026-10-01T10:00:00+00:00")
    assert rb.reset_retry_after(expired, now="2026-11-01T11:00:00+00:00") is None
    inside = _state(last_reset_at="2026-10-01T10:00:00+00:00")
    assert rb.reset_retry_after(inside, now="2026-10-30T10:00:00+00:00") == (
        "2026-10-31T10:00:00+00:00"
    )


# ------------------------------------------------------------------ 实盘 env 单一来源
def test_real_equity_env_derived_from_single_source():
    """Chief 硬要求：env 不写字面量，由 `REAL_ACCOUNT_ID` 派生（换账户自动跟随）。"""
    from types import SimpleNamespace

    settings = SimpleNamespace(real_account_id=281756481476169109)
    assert rb.real_equity_env(settings) == "REAL_281756481476169109"
    assert rb.real_equity_env(SimpleNamespace(real_account_id="281756481476169109")) == (
        "REAL_281756481476169109"
    )
    for bad in (None, 0, "", "  "):
        with pytest.raises(rb.BreakerConfigError, match="REAL_ACCOUNT_ID"):
            rb.real_equity_env(SimpleNamespace(real_account_id=bad))


def test_breaker_scope_only_real_and_simulate_explicitly_refused():
    """Chief 定案：本批只对 `REAL_{account_id}` 启用；SIMULATE 显式拒绝（不静默 off）。"""
    from types import SimpleNamespace

    settings = SimpleNamespace(real_account_id=281756481476169109)
    assert rb.resolve_equity_env(settings, "REAL") == "REAL_281756481476169109"
    assert rb.resolve_equity_env(settings, "real") == "REAL_281756481476169109"
    for env in ("SIMULATE", "simulate", "", None, "PAPER"):
        with pytest.raises(rb.BreakerUnsupportedEnv) as excinfo:
            rb.resolve_equity_env(settings, env)
        assert excinfo.value.code == "breaker_unsupported_env"
        # 页面必须能显示「未启用 + 启用条件」，禁止静默关闭
        assert "未启用" in excinfo.value.page_hint()
        assert "账户" in excinfo.value.page_hint()
    # REAL 但未配置账户 → 仍然 fail-closed（派生防不了配错）
    with pytest.raises(rb.BreakerConfigError, match="REAL_ACCOUNT_ID"):
        rb.resolve_equity_env(SimpleNamespace(real_account_id=None), "REAL")


#: 硬编码实盘 env 的字面量形态：`REAL_<6 位以上数字>`。
REAL_ENV_LITERAL_PATTERN = re.compile(r"REAL_\d{6,}")


def find_hardcoded_real_env_literals(source: str) -> list[int]:
    """返回 `source` 中**代码**里出现 `REAL_<数字>` 字面量的行号（1-based）。

    只看代码，不看注释：`# 事故示例 REAL_20170229` 这类说明是**文档**，不是缺陷。
    历史口径是拿正则扫原始文本，于是 Coder 在注释/docstring 里写事故示例就被判红，
    结果守卫被删注释绕过（守卫钉形状 → 被改成不钉）。
    这里改用 `tokenize`：跳过 `COMMENT` token，以及落在 **docstring** 里的 `STRING` token；
    其余 STRING/f-string 片段照扫 —— 代码里真写死账户号必须转红。
    """
    import ast
    import io
    import tokenize

    docstring_lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            start = first.value.lineno
            end = getattr(first.value, "end_lineno", None) or start
            docstring_lines.update(range(start, end + 1))

    offenders: list[int] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and token.start[0] in docstring_lines:
            continue
        for offset, text in enumerate(token.string.splitlines() or [""]):
            if REAL_ENV_LITERAL_PATTERN.search(text):
                offenders.append(token.start[0] + offset)
    return offenders


def test_real_env_literal_detector_flags_code_and_ignores_prose():
    """双向自测：代码里真写死 → 红；只写在注释/docstring 的示例 → 绿。

    这条自测是 ① 的变异证据：把 `find_hardcoded_real_env_literals` 退回
    「扫原始文本」，第一段断言仍绿、第二段立刻转红（注释被误判）；
    反之若把它改成「整份文件只要有 REAL_ 就跳过」，第一段转红。
    """
    prose_only = '''"""事故复盘：曾经手抄成 `REAL_20170229`（docstring 里的示例）。"""


def _doc() -> None:
    """再提一次：REAL_20170229 是错位示例，禁止硬编码。"""
    # 注释里提到 REAL_20170229 同样只是文档
    return None
'''
    assert find_hardcoded_real_env_literals(prose_only) == [], "注释/docstring 里的示例被误判为硬编码"

    code_hits = [
        'ENV = "REAL_20170229"\n',
        'ENV = f"REAL_20170229"\n',
        'def f():\n    return "REAL_20170229".lower()\n',
    ]
    assert find_hardcoded_real_env_literals(code_hits[0]) == [1]
    assert find_hardcoded_real_env_literals(code_hits[1]) == [1]
    assert find_hardcoded_real_env_literals(code_hits[2]) == [2]

    # 文档在前、代码在后：同文件里一旦真写进代码，行号必须指向代码行
    mixed = prose_only + 'ENV = f"REAL_20170229"\n'
    assert find_hardcoded_real_env_literals(mixed) == [prose_only.count("\n") + 1]


def test_no_hardcoded_real_account_env_literal_in_src():
    """守卫：`src/` 的**代码**里不得出现 `REAL_<数字>` 字面量（手抄错位就是假绿的入口）。"""
    from pathlib import Path

    import futu_quant

    src_root = Path(futu_quant.__file__).resolve().parent
    assert src_root.is_dir(), f"守卫读不到源码目录：{src_root}"
    paths = sorted(src_root.glob("*.py"))
    # 读不到输入时不能返回空结果（空 offenders 会伪装成"全绿"）
    assert len(paths) > 20, f"只扫到 {len(paths)} 个源文件，守卫输入异常：{src_root}"

    offenders: list[str] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert source.strip(), f"{path.name} 读成空文件，守卫输入异常"
        offenders.extend(
            f"{path.name}:{lineno}" for lineno in find_hardcoded_real_env_literals(source)
        )
    assert not offenders, (
        f"源码代码里出现硬编码实盘 env 字面量（注释/docstring 不算）：{offenders}"
        " —— 实盘 env 必须由 REAL_ACCOUNT_ID 派生（risk_breaker.real_equity_env）"
    )


def test_partial_reset_is_still_blocked_mutation():
    """V12 变异：三件套只做一件必须仍被拦（只重置 baseline → 绝对底线仍拦）。"""
    state = _state(breaker_level=2, peak_hist=100_000.0)
    only_baseline = rb.RiskState(
        **{**state.as_row(), "baseline_equity": 89_000.0, "peak_reset": 89_000.0,
           "breaker_level": 0, "updated_at": "2026-10-25T15:00:00"}
    )
    decision = rb.evaluate(only_baseline, rb.validate_equity_sample(89_000.0, None), TH,
                           now="2026-10-25T15:05:00", current_trading_day="2026-10-25")
    assert decision.level == 2  # 只重置 → dd_vs_peak 11% 仍触发绝对底线
