"""回归守卫：模拟盘账户带 account_id 时，派生限额必须读 SIMULATE 快照。

事故（2026-11-04）：`env = f"REAL_{account_id}" if account_id else "SIMULATE"`
把模拟盘（account_id=20170229）指向 `REAL_20170229`，而模拟盘权益全写在 `SIMULATE`，
导致派生限额取不到权益 → fail-closed → 模拟盘每一单都被拒。
"""

from __future__ import annotations

import json

import pytest

from futu_quant.limit_profile_store import profile_key
from futu_quant.risk_limits import RiskLimits, RiskLimitsConfigError, _apply_limit_profile
from futu_quant.tenant import TenantConfigKeyError, equity_snapshot_env


PAPER_ACCOUNT = "20170229"
REAL_ACCOUNT = "281756481476169109"


def test_equity_env_uses_shared_tenant_entrypoint():
    """环境必须由租户 trading_env 派生，不能按 account_id 猜测。"""
    assert equity_snapshot_env("futu_paper_sandbox", PAPER_ACCOUNT) == "SIMULATE"
    assert equity_snapshot_env("futu_real_master", REAL_ACCOUNT) == f"REAL_{REAL_ACCOUNT}"


def test_equity_env_unknown_tenant_with_account_fails_closed():
    """有账户但租户无法解析时必须拒绝，不得猜测环境。"""
    with pytest.raises(TenantConfigKeyError):
        equity_snapshot_env("unknown-tenant", "99999999")


class _Store:
    """最小替身：只有 SIMULATE 权益，没有 REAL_<paper_account>。"""

    def __init__(self, equity: float = 991945.32):
        self.equity = equity
        self.asked_envs: list[str] = []

    def get_setting(self, key, default=""):
        return default

    def get_runtime_config(self, key):
        if key == profile_key("futu_paper_sandbox"):
            return ({"per_order_ratio": 0.06, "daily_entries_ratio": 0.12,
                     "daily_loss_ratio": 0.06, "capacity_ratio": None}, 1)
        return (None, 0)

    def equity_history(self, limit=1, env=None):
        self.asked_envs.append(env)
        if env == "SIMULATE":
            return [{"total_assets": self.equity, "created_at": "2026-11-04T15:00:00+00:00"}]
        return []


def test_paper_derived_limits_read_simulate_snapshot():
    store = _Store()
    limits = RiskLimits(
        max_order_notional=2500.0, max_daily_notional=5000.0, max_daily_loss=100000.0,
    )
    result = _apply_limit_profile(store, "futu_paper_sandbox", limits, PAPER_ACCOUNT)

    assert "SIMULATE" in store.asked_envs, f"应查 SIMULATE，实际查询 {store.asked_envs}"
    assert f"REAL_{PAPER_ACCOUNT}" not in store.asked_envs
    # 派生生效：6% × 991945.32 ≈ 59516.72，被硬顶 50000 截断
    assert result.max_order_notional == pytest.approx(50000.0)
