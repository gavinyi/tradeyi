"""P0-2b 守卫：`RISK_LIMITS_ENV_FALLBACK` 巡检判红（Ops 2026-11-05）。

背景：这个计数只有 `audit_events` 一行 + 一个 runtime_config 计数器，**没有任何告警**
（Reviewer 实测：39 条 audit / 0 条 alert）。回落成环境变量默认额度时实盘照样下单，
没人知道 —— 属于「库里有、人看不见」。

判据（Chief 2026-11-05 08:25）：
- 按租户列出计数，出现即报；
- 日报里**固定一行**（绿也要出现「0 次」，否则"没这行"和"0 次"分不开）；
- **读库异常必须判红**，不许"查不到=绿"。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from futu_quant.store import TradingStore  # noqa: E402

pytest.importorskip("scripts.scheduled_log_audit")
audit = pytest.importorskip("scripts.scheduled_log_audit")

EVENT = "RISK_LIMITS_ENV_FALLBACK"
TENANT_A = "futu_real_master"
TENANT_B = "futu_paper_sandbox"


def _store(tmp_path) -> TradingStore:
    return TradingStore(tmp_path / "patrol.db")


def _record(store, tenant: str, account: str | None = "281756481476169109") -> None:
    store.audit(
        EVENT,
        {
            "account_id": account,
            "tenant_id": tenant,
            "source": "env",
            "message": "账户级与租户级风控配置均缺失，回落 env 全局默认（迁移前状态）",
        },
        actor="service:risk",
    )


def _age_out(store, days: int = 2) -> None:
    """把已写入的审计行改成 N 天前，用来测窗口边界。"""
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with store.connection() as connection:
        connection.execute("UPDATE audit_events SET created_at=? WHERE event_type=?", (stamp, EVENT))


# --------------------------------------------------------------------------- #
# 判红
# --------------------------------------------------------------------------- #


def test_event_present_turns_patrol_red_with_tenant_counts(tmp_path):
    store = _store(tmp_path)
    _record(store, TENANT_A)
    _record(store, TENANT_A)
    issues = audit.check_risk_limits_env_fallback(store)
    assert len(issues) == 1, "出现即报，且只报一条（聚合在文案里）"
    text = issues[0]
    assert TENANT_A in text, "必须点名租户，否则不知道去补谁的配置"
    assert "×2" in text, "必须给出次数"
    assert "回落" in text


def test_zero_events_is_green_but_line_still_shows_zero(tmp_path):
    store = _store(tmp_path)
    assert audit.check_risk_limits_env_fallback(store) == []
    line = audit.risk_limits_env_fallback_line(store)
    assert "0 次" in line, "绿的时候也要有这一行，否则看不出巡检到底跑没跑"


def test_tenants_are_reported_separately(tmp_path):
    store = _store(tmp_path)
    _record(store, TENANT_A)
    _record(store, TENANT_A)
    _record(store, TENANT_A)
    _record(store, TENANT_B)
    text = audit.check_risk_limits_env_fallback(store)[0]
    assert f"{TENANT_A} / 281756481476169109 ×3" in text
    assert f"{TENANT_B} / 281756481476169109 ×1" in text, "跨租户不得合并成一条计数"


def test_events_outside_window_are_ignored(tmp_path):
    store = _store(tmp_path)
    _record(store, TENANT_A)
    _age_out(store, days=2)
    assert audit.check_risk_limits_env_fallback(store) == []
    assert "0 次" in audit.risk_limits_env_fallback_line(store)


# --------------------------------------------------------------------------- #
# fail-closed：读不到 ≠ 0
# --------------------------------------------------------------------------- #


def test_read_failure_is_red_not_green(tmp_path):
    """读库炸了必须判红。判绿就等于把"看不到"当成"没有"。"""
    store = _store(tmp_path)

    class Broken:
        def connection(self):
            raise RuntimeError("模拟 audit_events 读取失败")

    issues = audit.check_risk_limits_env_fallback(Broken())
    assert len(issues) == 1
    assert "读取失败" in issues[0]
    assert "不能判绿" in issues[0]

    line = audit.risk_limits_env_fallback_line(Broken())
    assert "读取失败" in line and "不能判绿" in line


def test_missing_tenant_field_falls_back_to_placeholder(tmp_path):
    """旧记录没有 tenant_id 时不能消失，要显示成占位符并计入红。"""
    store = _store(tmp_path)
    _record(store, None)  # type: ignore[arg-type]
    text = audit.check_risk_limits_env_fallback(store)[0]
    assert "<未知租户>" in text
