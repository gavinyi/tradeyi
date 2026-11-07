"""熔断 ack UI 化守卫（P2-①，2026-11-07，红区）。

PRD 验收（prd_p2_batch_20261107.md §1.5）逐条：
零行为差异 / 缺理由或密码错不可 ack / 无批量入口 / paper-real 隔离 /
ack 链路零配置写入 / 记录可追溯 + 当次有效（禁永久豁免）。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from futu_quant import breaker_ack as ba  # noqa: F401
from futu_quant.breaker_ack import AckRequest, BreakerAckLog, ack_breaker

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "futu_quant" / "breaker_ack.py"
PAGE = ROOT / "pages" / "15_strategy_control_center.py"


class FakeAlerts:
    def __init__(self, store):
        self.store = store
        self.recovered: list[tuple] = []

    def recover(self, event_type, message, incident_key=None):
        self.recovered.append((event_type, message, incident_key))


class FakeStore:
    """最小 store 替身：只支持 ack 链路需要的读写（含 now()）。"""

    def __init__(self):
        self.settings: dict[str, str] = {}
        self._now = "2026-11-07T15:30:00+00:00"

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def now(self):
        return self._now


def _req(**overrides) -> AckRequest:
    import os
    from futu_quant.admin_auth import hash_password

    os.environ["ADMIN_PASSWORD_HASH"] = hash_password("right-password")
    base = dict(
        tenant_id="futu_paper_sandbox", incident_key="LEDGER_MISMATCH",
        incident_version="2026-11-07T15:00:00+00:00",
        reason="已核对持仓与账本，确认无资金缺口，继续观察",
        admin_password="right-password", confirmed=True,
    )
    base.update(overrides)
    return AckRequest(**base)


# ---------- 三要素（PRD 1.3） ----------

def test_short_reason_rejected():
    with pytest.raises(PermissionError) as exc:
        ack_breaker(FakeStore(), FakeAlerts(FakeStore()),
                    _req(reason="太短"), incident_open=True)
    assert "理由" in str(exc.value)


def test_wrong_password_rejected():
    with pytest.raises(PermissionError) as exc:
        ack_breaker(FakeStore(), FakeAlerts(FakeStore()),
                    _req(admin_password="wrong"), incident_open=True)
    assert "密码" in str(exc.value)


def test_missing_confirmation_rejected():
    with pytest.raises(PermissionError) as exc:
        ack_breaker(FakeStore(), FakeAlerts(FakeStore()),
                    _req(confirmed=False), incident_open=True)
    assert "二次确认" in str(exc.value)


def test_closed_incident_rejected():
    with pytest.raises(PermissionError) as exc:
        ack_breaker(FakeStore(), FakeAlerts(FakeStore()), _req(), incident_open=False)
    assert "无需 ack" in str(exc.value)


# ---------- 零行为差异 + 零配置写入（PRD 1.5-1/5） ----------

def test_ack_only_recovers_and_logs_no_config_write():
    store = FakeStore()
    alerts = FakeAlerts(store)
    entry = ack_breaker(store, alerts, _req(), incident_open=True)
    # 零行为差异：动作 = recover 同一 incident_key
    assert alerts.recovered and alerts.recovered[0][0] == "LEDGER_MISMATCH"
    assert alerts.recovered[0][2] == "LEDGER_MISMATCH"
    # 零配置写入：settings 里只允许 ack 记录键
    from futu_quant.breaker_ack import ACK_LOG_KEY
    written = set(store.settings)
    assert written == {ACK_LOG_KEY.format(tenant="futu_paper_sandbox")}, written
    assert entry["reason"].startswith("已核对")


def test_ast_guard_no_config_write_in_ack_path():
    """AST 守卫（PRD 1.5-5 必红）：ack 模块禁止 set_setting 之外的任何配置写入。"""
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "ack_breaker")
    seg = ast.get_source_segment(source, fn)
    for banned in ("set_strategy_enabled", "set_tenant_setting", "upsert_campaign",
                   "set_strategy_entries_paused", "mutate_runtime_config",
                   "save_", "place_limit", "submit_limit"):
        assert banned not in seg, f"ack 链路出现配置/交易写入点：{banned}"


# ---------- 当次有效（禁永久豁免） ----------

def test_ack_is_per_incident_version():
    store = FakeStore()
    ack_breaker(store, FakeAlerts(store), _req(), incident_open=True)
    log = BreakerAckLog(store, "futu_paper_sandbox")
    assert log.has_ack_for("LEDGER_MISMATCH", "2026-11-07T15:00:00+00:00")
    # 新版本（再次触发）≠ 已 ack：禁永久豁免
    assert not log.has_ack_for("LEDGER_MISMATCH", "2026-11-08T09:00:00+00:00")
    assert not log.has_ack_for("OTHER_MISMATCH", "2026-11-07T15:00:00+00:00")


# ---------- 跨租户隔离（PRD 1.4） ----------

def test_paper_ack_does_not_touch_real():
    store = FakeStore()
    ack_breaker(store, FakeAlerts(store),
                _req(tenant_id="futu_paper_sandbox"), incident_open=True)
    real_log = BreakerAckLog(store, "futu_real_master")
    assert real_log.entries() == [], "paper 的 ack 不得出现在 real 的记录里"


# ---------- 无批量入口（PRD 1.5-3） ----------

def test_no_batch_ack_entry():
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    funcs = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef,))]
    assert not any("batch" in name or "all" in name.lower() and "ack" in name
                   for name in funcs), f"存在批量 ack 形态的函数：{funcs}"
    assert "ack_breaker" in funcs  # 单对象接口存在
    # 页面也不得出现批量按钮
    page = PAGE.read_text(encoding="utf-8")
    assert "全部确认" not in page and "批量" not in page


# ---------- 页面三要素与确认文案（PRD 1.2/1.3） ----------

def test_page_has_three_factors_and_confirm_phrase():
    page = PAGE.read_text(encoding="utf-8")
    assert "breaker_ack_reason" in page, "理由输入缺失"
    assert 'type="password"' in page, "管理员密码安全输入缺失"
    assert "breaker_ack_confirm" in page, "二次确认缺失"
    from futu_quant.breaker_ack import CONFIRM_ACK_PHRASE
    assert CONFIRM_ACK_PHRASE in page, "二次确认必须明写 ack 不改变任何限额"


def test_page_ack_button_disabled_without_reason():
    page = PAGE.read_text(encoding="utf-8")
    assert "disabled=(len(_ack_reason.strip()) < 10" in page, \
        "理由不足时按钮必须禁用"


# ---------- 可追溯（PRD 1.5-6） ----------

def test_ack_log_entries_have_audit_fields():
    store = FakeStore()
    ack_breaker(store, FakeAlerts(store), _req(), incident_open=True,
                context={"severity": "CRITICAL"})
    entry = BreakerAckLog(store, "futu_paper_sandbox").entries()[0]
    for field in ("actor", "tenant_id", "incident_key", "incident_version",
                  "reason", "context", "acked_at"):
        assert field in entry, f"ack 记录缺审计字段 {field}"
