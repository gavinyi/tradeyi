"""时区到期判定 + 硬诊断只拦开仓（`fix/timezone-expiry`）。

两条判据都必须**能失败**：

① 「北京已跨日、纽约仍是当日」时，当日到期合约**不得**被判「已到期」。
   判据用固定时刻而不是当前时间 —— 否则这条测试只在一天中的某几个小时有意义。
② 命中硬诊断且**有持仓**时，退出管理**不得**被阻断。
   判据钉的是「`quantity` 在何时才是权威值」：把门放回函数开头，收到的会是 0，
   有持仓的 campaign 就会被当成开仓拦掉 —— 这正是要修的缺陷。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import futu_quant.gamma_debug as gd

NY = ZoneInfo("America/New_York")

# 北京 2026-10-24 00:30 == UTC 2026-10-23 16:30 == 纽约 2026-10-23 12:30
INSTANT = datetime(2026, 10, 23, 16, 30, tzinfo=timezone.utc)
BEIJING_TODAY = date(2026, 10, 24)
NY_TODAY = date(2026, 10, 23)


class _FrozenDatetime(datetime):
    """冻结 `datetime.now(tz)`，让「北京跨日/纽约当日」可复现。"""

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return INSTANT.astimezone(tz) if tz is not None else INSTANT.replace(tzinfo=None)


class _BeijingDate(date):
    """`date.today()` 返回**北京日期** —— 即修复前代码的取值来源。"""

    @classmethod
    def today(cls):  # noqa: D102
        return BEIJING_TODAY


# --------------------------------------------------------------------------
# ① T-0 不得进 hard_issues
# --------------------------------------------------------------------------


def test_beijing_crossed_day_does_not_flag_expiry(monkeypatch):
    """北京 10-24 00:30 / 纽约 10-23 12:30 → 10-23 到期的合约仍在纽约当日，不得判已到期。"""
    monkeypatch.setattr(gd, "datetime", _FrozenDatetime)
    monkeypatch.setattr(gd, "date", _BeijingDate)

    # 夹具自检：必须让「北京日期」真的比「纽约日期」大，否则这条测试测不出回归。
    assert _BeijingDate.today() > NY_TODAY, "夹具失效：北京日期没有跨日"
    # 并且旧判据（date.today()）**确实会**命中 —— 这就是回归会被抓到的原因。
    assert NY_TODAY < _BeijingDate.today()

    campaign = {"code": "US.META261023P660000", "trigger_mode": "VOLATILITY"}
    issues = gd.diagnose_campaign(campaign, [])

    assert not any("合约已于" in issue for issue in issues), (
        f"纽约当日到期的合约被误判为已到期（说明用了北京日期）：{issues}"
    )


def test_already_expired_still_flagged(monkeypatch):
    """反向判据：**真的**过了纽约日期的合约必须仍然被判已到期 —— 不能为了修误判而漏判。"""
    monkeypatch.setattr(gd, "datetime", _FrozenDatetime)
    monkeypatch.setattr(gd, "date", _BeijingDate)

    campaign = {"code": "US.META261022P660000", "trigger_mode": "VOLATILITY"}
    issues = gd.diagnose_campaign(campaign, [])

    assert any("合约已于" in issue for issue in issues)


# --------------------------------------------------------------------------
# ② 硬诊断只拦新开仓，不拦退出管理
# --------------------------------------------------------------------------


def test_hard_issues_block_only_new_entries():
    from scripts.run_gamma_strategy import hard_issues_block_action

    assert hard_issues_block_action(["财报时间未人工确认"], 0) is True
    assert hard_issues_block_action(["财报时间未人工确认"], -1) is True
    assert hard_issues_block_action(["财报时间未人工确认"], 1) is False, "有持仓 = 退出管理，不得阻断"
    assert hard_issues_block_action([], 1) is False
    assert hard_issues_block_action([], 0) is False


def test_run_campaign_gate_receives_resolved_position_quantity(monkeypatch):
    """钉住判据位置：`run_campaign` 必须把**已解析的持仓量**交给判据。

    判据若放回函数开头（修复前的形态），这里收到的会是 0 —— 有持仓的 campaign
    会被当成开仓拦掉，退出管理被冻结。本用例用一个持仓量=1 的 broker 直接驱动
    `run_campaign`，断言判据收到的是 1。
    """
    import scripts.run_gamma_strategy as rgs

    code = "US.META261030P660000"
    positions = pd.DataFrame([{"code": code, "qty": 1, "cost_price": 2.0}])

    class FakeBroker:
        def positions(self, refresh=False):
            return positions

        def orders(self, *args, **kwargs):
            return pd.DataFrame()

    class FakeStore:
        def earnings_events(self):
            return []

        def get_setting(self, key, default=""):
            return default

        def decision(self, *args, **kwargs):
            return None

        def decision_if_changed(self, *args, **kwargs):
            return None

        def audit(self, *args, **kwargs):
            return None

    class FakeExecutor:
        store = FakeStore()

    captured: dict = {}

    def _gate(hard_issues, quantity):
        captured["hard"] = list(hard_issues)
        captured["quantity"] = int(quantity)
        return True  # 强制早退：只验判据入参，不进入后续深链路

    monkeypatch.setattr(rgs, "diagnose_campaign", lambda campaign, events: ["财报时间未人工确认"])
    monkeypatch.setattr(rgs, "hard_campaign_issues", lambda campaign, issues: list(issues))
    monkeypatch.setattr(rgs, "hard_issues_block_action", _gate)

    campaign = {
        "code": code,
        "trigger_mode": "VOLATILITY",
        "underlying": "META",
        "legs": [{"code": code, "side": "BUY", "ratio": 1}],
    }
    rgs.run_campaign(FakeBroker(), FakeExecutor(), campaign, {})

    assert captured.get("hard") == ["财报时间未人工确认"], "硬诊断没有传到判据"
    assert captured.get("quantity") == 1, (
        f"判据收到的持仓量是 {captured.get('quantity')} —— 说明它被放在了持仓解析之前，"
        "有持仓的 campaign 会被当成开仓拦掉"
    )
