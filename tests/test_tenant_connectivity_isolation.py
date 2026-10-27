"""租户连接降级隔离守卫（2026-10-27 事故）。

事故形态：实盘账户从券商侧消失 → 全局 `connectivity_degraded=true` →
**模拟租户的 BUY 也被拦**，表现为「实盘一个账号出问题，模拟盘没进度」。

本文件的守卫针对读取侧 `futu_quant.risk.connectivity_degraded_for`：
租户键存在即以其为准，无前缀全局键只在租户键缺失时兜底。

变异可红：删掉 `connectivity_degraded_for` 里的租户键分支（直接读全局键），
`test_paper_not_blocked_by_real_tenant_degradation` 与
`test_tenant_false_overrides_global_true` 会转红。
"""

from __future__ import annotations

import pytest

from futu_quant.risk import connectivity_degraded_for
from futu_quant.store import TradingStore

PAPER_ACCOUNT = "20170229"
REAL_ACCOUNT = "281756481476169109"


class _Broker:
    """最小 broker 替身：只需 trading_env + account_id 供 tenant_of_broker 反查。"""

    def __init__(self, trading_env: str, account_id: str) -> None:
        self.trading_env = trading_env
        self.account_id = account_id


@pytest.fixture()
def store(tmp_path):
    return TradingStore(tmp_path / "isolation.db")


def test_paper_not_blocked_by_real_tenant_degradation(store):
    """实盘租户降级 + 全局降级，模拟租户仍可下单（事故复现场景）。"""
    store.set_setting("connectivity_degraded", "true")
    store.set_setting("connectivity_degraded:futu_real_master", "true")
    store.set_setting("connectivity_degraded:futu_paper_sandbox", "false")

    assert connectivity_degraded_for(store, _Broker("SIMULATE", PAPER_ACCOUNT)) is False
    assert connectivity_degraded_for(store, _Broker("REAL", REAL_ACCOUNT)) is True


def test_tenant_false_overrides_global_true(store):
    store.set_setting("connectivity_degraded", "true")
    store.set_setting("connectivity_degraded:futu_paper_sandbox", "false")

    assert connectivity_degraded_for(store, _Broker("SIMULATE", PAPER_ACCOUNT)) is False


def test_tenant_true_wins_even_when_global_false(store):
    store.set_setting("connectivity_degraded", "false")
    store.set_setting("connectivity_degraded:futu_real_master", "true")

    assert connectivity_degraded_for(store, _Broker("REAL", REAL_ACCOUNT)) is True


def test_falls_back_to_global_when_tenant_key_absent(store):
    """迁移期兜底：租户键还没写过时，行为与改造前一致（不放松）。"""
    store.set_setting("connectivity_degraded", "true")

    assert connectivity_degraded_for(store, _Broker("SIMULATE", PAPER_ACCOUNT)) is True


def test_unresolvable_broker_uses_global(store):
    """解析不出租户（未注册账户）时仍按全局判定，绝不静默放行。"""
    store.set_setting("connectivity_degraded", "true")

    assert connectivity_degraded_for(store, _Broker("SIMULATE", "99999999")) is True
