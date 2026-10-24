"""P1-8：`PORTFOLIO_STRESS` 告警租户化 + 双阈值滞回。

守四件事：
1. 阈值单源：租户键 → 旧无前缀键 → env → 默认；
2. 非法阈值（含解析失败、recover>=breach）→ **落 ERROR 并用安全默认值**，不静默；
3. incident 键租户化（实盘/模拟盘不再互相顶掉）；
4. 滞回：缓冲带内**不重发告警也不误消警**；状态持久化，重启后不会重复告警。

变异：把 `next_stress_action` 的缓冲带判断去掉（回到单阈值）→ 序列用例转红；
把非法阈值分支改成静默返回默认（不落事件）→ 非法用例转红。
"""

from __future__ import annotations

from futu_quant.store import TradingStore
from futu_quant.stress_alerts import (
    DEFAULT_BREACH,
    DEFAULT_RECOVER,
    LEGACY_INCIDENT_KEY,
    STATE_ALERTING,
    STATE_OK,
    next_stress_action,
    portfolio_stress_incident_key,
    read_hysteresis_state,
    resolve_portfolio_stress_thresholds,
    write_hysteresis_state,
)

REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"


def test_threshold_defaults_and_tenant_override(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    assert resolve_portfolio_stress_thresholds(store, REAL) == (DEFAULT_BREACH, DEFAULT_RECOVER)

    store.set_setting(f"portfolio_stress_alert_pct:{REAL}", "0.15")
    store.set_setting(f"portfolio_stress_recover_pct:{REAL}", "0.12")
    assert resolve_portfolio_stress_thresholds(store, REAL) == (0.15, 0.12)
    # 租户隔离：模拟盘仍走默认
    assert resolve_portfolio_stress_thresholds(store, PAPER) == (DEFAULT_BREACH, DEFAULT_RECOVER)


def test_invalid_threshold_falls_back_visibly(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    # recover >= breach 是非法组合
    store.set_setting(f"portfolio_stress_alert_pct:{REAL}", "0.05")
    store.set_setting(f"portfolio_stress_recover_pct:{REAL}", "0.06")

    breach, recover = resolve_portfolio_stress_thresholds(store, REAL)

    assert (breach, recover) == (DEFAULT_BREACH, DEFAULT_RECOVER)
    events = [
        event for event in store.operational_events(limit=50)
        if event["event_type"] == "PORTFOLIO_STRESS_THRESHOLD_INVALID"
    ]
    assert events, "非法阈值必须落 ERROR 事件，否则「配置写错」会表现成「告警再也不响」"
    assert events[0]["status"] == "ERROR"


def test_incident_key_is_tenant_scoped(tmp_path):
    assert portfolio_stress_incident_key(REAL) == f"{LEGACY_INCIDENT_KEY}:{REAL}"
    assert portfolio_stress_incident_key(PAPER) == f"{LEGACY_INCIDENT_KEY}:{PAPER}"
    # 无租户上下文时保留旧键（单向兼容）
    assert portfolio_stress_incident_key(None) == LEGACY_INCIDENT_KEY


def test_hysteresis_sequence_prevents_flapping():
    """0.102 告警后，9.9%/9.5% 在缓冲带内必须静默；跌破 8% 才消警。"""
    breach, recover = 0.10, 0.08
    state = STATE_OK
    actions = []
    for ratio in (0.09, 0.102, 0.099, 0.095, 0.078):
        action = next_stress_action(ratio, state, breach, recover)
        actions.append(action)
        if action == "alert":
            state = STATE_ALERTING
        elif action == "recover":
            state = STATE_OK
    assert actions == ["none", "alert", "none", "none", "recover"], (
        f"滞回序列不符（会 flapping）：{actions}"
    )


def test_hysteresis_state_persists_per_tenant(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    write_hysteresis_state(store, REAL, STATE_ALERTING)
    assert read_hysteresis_state(store, REAL) == STATE_ALERTING
    # 另一个租户不受影响
    assert read_hysteresis_state(store, PAPER) == ""
    write_hysteresis_state(store, REAL, STATE_OK)
    assert read_hysteresis_state(store, REAL) == STATE_OK


def test_sustained_breach_does_not_realert():
    """语义钉死：一条 incident 只发一次，恢复时发一次 recover。

    ALERTING 期间持续破线不得每轮重发（否则 record_incident 的 count 每轮 +1，
    用户看到的是"一直在报警"，反而分不清是新事故还是同一次）。
    """
    breach, recover = 0.10, 0.08
    assert next_stress_action(0.15, STATE_OK, breach, recover) == "alert"
    assert next_stress_action(0.15, STATE_ALERTING, breach, recover) == "none"
    assert next_stress_action(0.102, STATE_ALERTING, breach, recover) == "none"
    # 跌破恢复线仍必须消警
    assert next_stress_action(0.05, STATE_ALERTING, breach, recover) == "recover"

