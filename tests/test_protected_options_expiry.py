"""持仓保护模式的「未平期权」判定：已到期合约不得参与。

2026-10-27 事故：本地账本残留 9 张 8 月到期的期权（`US.SPCX260814C1xxxxx`、
`US.NVDA260807Cxxxxx` 等），`active_protected_option_positions` 把它们当成「未平期权」，
于是**每次 OpenD 抖动都触发持仓保护模式** → 暂停新开仓 + 重启整个服务。

变异可红：删掉 `connectivity_protection.py` 里 `contract.expiration < _today_new_york()`
那段跳过逻辑，`test_expired_contracts_are_ignored` 会转红。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from futu_quant.connectivity_protection import active_protected_option_positions
from futu_quant.store import TradingStore

NEW_YORK = ZoneInfo("America/New_York")


def _contract_code(days_from_now: int) -> str:
    day = datetime.now(NEW_YORK).date() + timedelta(days=days_from_now)
    return f"US.QQQ{day.strftime('%y%m%d')}C735000"


@pytest.fixture()
def store(tmp_path):
    return TradingStore(tmp_path / "protection.db")


def test_expired_contracts_are_ignored(store):
    expired = _contract_code(-80)   # 8 月到期那批的等价物
    live = _contract_code(3)
    store.set_virtual_allocation("volatility_long_gamma", expired, 1.0, 1.0)
    store.set_virtual_allocation("volatility_long_gamma", live, 1.0, 1.0)

    symbols = [row["symbol"] for row in active_protected_option_positions(store)]

    assert symbols == [live], f"已到期合约不应参与持仓保护，实际={symbols}"


def test_all_expired_means_no_protection(store):
    store.set_virtual_allocation("volatility_long_gamma", _contract_code(-200), 1.0, 1.0)
    store.set_virtual_allocation("volatility_long_gamma", _contract_code(-80), -1.0, 1.0)

    assert active_protected_option_positions(store) == []


def test_live_contract_still_protected(store):
    live = _contract_code(2)
    store.set_virtual_allocation("volatility_long_gamma", live, 1.0, 1.0)

    assert [row["symbol"] for row in active_protected_option_positions(store)] == [live]
