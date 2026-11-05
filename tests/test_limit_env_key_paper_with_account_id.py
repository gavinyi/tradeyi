"""P0 回归守卫：模拟租户**也有** account_id 时，派生限额必须读 `SIMULATE` 净值快照。

2026-11-04 生产缺陷（`v2026.11.04-2` 带进）：`risk_limits._apply_limit_profile` 用
`f"REAL_{account_id}" if account_id else "SIMULATE"` 猜净值快照的 env，而模拟账户
（`futu_paper_sandbox`，account_id=20170229）的快照写的是 `SIMULATE`：
- `env=REAL_20170229` → 0 行
- `env=SIMULATE` → 有行
后果：派生日亏线静默回退到 .env 的 $10,000，`RISK_LIMITS_CONFIG_INCOMPLETE` 走
fail-closed 直接拒单 —— 模拟盘开着跑却发不出单。

守卫两向：模拟必须取到真实权益（不回退），实盘仍取 `REAL_<account_id>`。
"""
import json
from types import SimpleNamespace

import pytest

from futu_quant.limit_profile import LimitProfile
from futu_quant.limit_profile_store import publish_limit_profile
from futu_quant.risk_limits import (
    clear_risk_limits_cache,
    resolve_risk_limits,
    risk_limits_tenant_key,
)
from futu_quant.store import TradingStore
from futu_quant.tenant import TenantConfigKeyError, equity_snapshot_env

PAPER_TENANT = "futu_paper_sandbox"
PAPER_ACCOUNT = "20170229"
REAL_TENANT = "futu_real_master"
REAL_ACCOUNT = "281756481476169109"

PAPER_EQUITY = 6_671.0  # 20% → 1334.20（2026-11-05 用户拍板三参 0.20）
REAL_EQUITY = 1_000_000.0

ENV_DEFAULT_DAILY_LOSS = 10_000.0  # .env 固定值（静默回退时的值）


def _complete_risk_section():
    return {
        "max_entry_notional": 2_500.0,
        "max_entry_daily_notional": 5_000.0,
        "max_reduction_order_notional": 150_000.0,
        "max_daily_reduction_notional": 500_000.0,
        "max_pending_orders": 20,
        "max_symbol_weight": 0.25,
        "max_gross_exposure": 0.85,
        "max_option_premium": 30_000.0,
        "max_daily_loss": 100_000.0,
        "max_drawdown_pct": 0.05,
    }


def _ready_store(tmp_path, *, tenant: str, env: str, equity: float) -> TradingStore:
    store = TradingStore(tmp_path / f"{tenant}-{env}.db")
    store.record_equity(equity, 0.0, equity, env=env)
    publish_limit_profile(
        store, tenant, LimitProfile(), "streamlit_admin:test", "派生限额",
        legacy_limits=SimpleNamespace(
            max_order_notional=2_500.0, max_daily_notional=5_000.0, max_daily_loss=100_000.0
        ),
        equity=equity, approval_fingerprint="fp-env-key", confirm=True,
    )
    store.set_setting(risk_limits_tenant_key(tenant), json.dumps(_complete_risk_section()))
    clear_risk_limits_cache()
    return store


def _scope(tenant: str, account_id: str) -> SimpleNamespace:
    return SimpleNamespace(account_id=account_id, tenant_id=tenant)


# ------------------------------------------------------------------ 派生入口本身

def test_equity_env_uses_tenant_mode_not_account_id():
    """模拟租户即使有 account_id，也必须派生成 SIMULATE。"""
    assert equity_snapshot_env(PAPER_TENANT, PAPER_ACCOUNT) == "SIMULATE"
    assert equity_snapshot_env(PAPER_TENANT, "") == "SIMULATE"
    assert equity_snapshot_env(REAL_TENANT, REAL_ACCOUNT) == f"REAL_{REAL_ACCOUNT}"


def test_equity_env_refuses_to_guess_for_unknown_account():
    """有 account_id 却解析不出租户时必须报错，不许猜 SIMULATE / REAL_<acc>。"""
    with pytest.raises(TenantConfigKeyError):
        equity_snapshot_env("no_such_tenant", "99999999")


# ----------------------------------------------------- 模拟盘：必须取到真实权益

def test_paper_tenant_with_account_id_reads_simulate_equity(tmp_path):
    store = _ready_store(tmp_path, tenant=PAPER_TENANT, env="SIMULATE", equity=PAPER_EQUITY)

    # 先钉住缺陷本身：旧写法拼出来的键在这个库里确实是 0 行
    assert store.equity_history(limit=1, env=f"REAL_{PAPER_ACCOUNT}") == []
    assert store.equity_history(limit=1, env="SIMULATE")

    # 模拟租户 + 有 account_id：必须按 SIMULATE 的真实权益派生，且不抛错
    # （不抛 RiskLimitsConfigError ⇒ 下游不会产生 RISK_LIMITS_CONFIG_INCOMPLETE 拒单）
    limits = resolve_risk_limits(store, _scope(PAPER_TENANT, PAPER_ACCOUNT))

    assert limits.max_order_notional == pytest.approx(1334.20, abs=0.02)
    assert limits.max_order_notional != pytest.approx(ENV_DEFAULT_DAILY_LOSS)


def test_paper_tenant_without_account_id_still_reads_simulate(tmp_path):
    """没 account_id 的模拟路径不许被这次改动弄坏。"""
    store = _ready_store(tmp_path, tenant=PAPER_TENANT, env="SIMULATE", equity=PAPER_EQUITY)

    limits = resolve_risk_limits(store, _scope(PAPER_TENANT, ""))

    assert limits.max_order_notional == pytest.approx(1334.20, abs=0.02)


# ------------------------------------------------------------ 实盘：不受影响

def test_real_tenant_reads_real_account_equity(tmp_path):
    store = _ready_store(
        tmp_path, tenant=REAL_TENANT, env=f"REAL_{REAL_ACCOUNT}", equity=REAL_EQUITY
    )

    # 实盘键有行、模拟键没行 —— 实盘不许被改成读 SIMULATE
    assert store.equity_history(limit=1, env=f"REAL_{REAL_ACCOUNT}")
    assert store.equity_history(limit=1, env="SIMULATE") == []

    limits = resolve_risk_limits(store, _scope(REAL_TENANT, REAL_ACCOUNT))

    # 实盘按 100 万权益派生（per_order 6% = 60k，再被 capacity_line 压到 5% = 50k），
    # 只要证明「用的是真实权益、不是 .env 的 10k 回退」即可，不钉死内部取 min 的细节。
    assert limits.max_order_notional > 40_000.0
    assert limits.max_order_notional != pytest.approx(ENV_DEFAULT_DAILY_LOSS)
