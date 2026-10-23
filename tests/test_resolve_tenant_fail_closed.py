"""``resolve_tenant`` 的 fail-closed 不变量（P0-9）。

**背景**：``resolve_tenant`` 曾在"无 target / account_id / env"时静默返回
**模拟租户**（``futu_paper_sandbox``）。那是 fail-open —— ``resolve_tenant(None)``
或 ``resolve_tenant(broker.tenant_id)``（生产 ``FutuPaperBroker`` 无该属性 → 恒空）
会静默拿到模拟租户，写侧于是把键写成 ``:{futu_paper_sandbox}``，跨租户污染。

**定案（Chief/Reviewer）**：无上下文时**抛异常**；未知 env 也抛异常。
刻意**不做静态源码守卫** —— 运行时检查约束不变量本身，而不是"哪种调用形态算合法"。

本文件把这条不变量钉成守卫：**该抛的必须抛，该通的必须通**（反向守卫防"改松"）。
"""

from __future__ import annotations

import pytest

from futu_quant.tenant import DEFAULT_TENANTS, resolve_tenant, tenant_of_broker

REAL_TENANT = "futu_real_master"
PAPER_TENANT = "futu_paper_sandbox"
REAL_ACCOUNT = str(DEFAULT_TENANTS[REAL_TENANT].account_id)
PAPER_ACCOUNT = str(DEFAULT_TENANTS[PAPER_TENANT].account_id)


class _Broker:
    """最小 broker 替身：带 account_id，**不带** tenant_id（与生产一致）。"""

    def __init__(self, account_id=None, trading_env=None):
        if account_id is not None:
            self.account_id = account_id
        if trading_env is not None:
            self.trading_env = trading_env


# --------------------------------------------------------------------------
# 该抛的必须抛（fail-closed）
# --------------------------------------------------------------------------


def test_no_context_raises():
    """三者皆无 → 抛异常，绝不返回默认租户。"""
    with pytest.raises(ValueError, match="三者皆无"):
        resolve_tenant()


def test_none_target_raises():
    """``resolve_tenant(None)`` —— 旧的 fail-open 入口。"""
    with pytest.raises(ValueError, match="三者皆无"):
        resolve_tenant(None)


def test_empty_string_target_raises():
    """空字符串同样等于"没给"。"""
    with pytest.raises(ValueError):
        resolve_tenant("")


def test_blank_string_target_raises():
    with pytest.raises(ValueError):
        resolve_tenant("   ")


def test_dead_broker_attribute_raises():
    """生产形态：broker 无 tenant_id 属性 → 拿不到任何上下文 → 抛。

    这正是 P0-9 定案要堵的：以前这里会静默返回**模拟租户**。
    """
    broker = _Broker(account_id=None, trading_env=None)
    with pytest.raises(ValueError):
        resolve_tenant(getattr(broker, "tenant_id", None))


def test_unknown_env_raises():
    """未知环境同样不猜 —— 旧行为会把它当成模拟盘。"""
    with pytest.raises(ValueError, match="未知交易环境"):
        resolve_tenant(env="NOPE")


def test_registry_without_default_env_tenant_raises():
    """注册表里没有该环境的默认租户 → 抛，而不是返回 None。"""
    with pytest.raises(ValueError):
        resolve_tenant(env="REAL", registry={})


# --------------------------------------------------------------------------
# 该通的必须通（反向守卫 —— 防止把检查"改松"或误伤合法调用）
# --------------------------------------------------------------------------


def test_explicit_tenant_id_still_resolves():
    assert resolve_tenant(REAL_TENANT).tenant_id == REAL_TENANT


def test_tenant_profile_passthrough():
    profile = DEFAULT_TENANTS[REAL_TENANT]
    assert resolve_tenant(profile) is profile


def test_env_real_still_resolves():
    assert resolve_tenant(env="REAL").tenant_id == REAL_TENANT


def test_env_simulate_still_resolves():
    assert resolve_tenant(env="SIMULATE").tenant_id == PAPER_TENANT
    assert resolve_tenant(env="PAPER").tenant_id == PAPER_TENANT


def test_account_id_still_resolves():
    assert resolve_tenant(account_id=REAL_ACCOUNT).tenant_id == REAL_TENANT
    assert resolve_tenant(account_id=PAPER_ACCOUNT).tenant_id == PAPER_TENANT


def test_account_id_plus_env_still_resolves():
    """``store.virtual_positions`` 的调用形态：``(None, account_id=..., env=...)``。"""
    assert resolve_tenant(None, account_id=REAL_ACCOUNT, env="REAL").tenant_id == REAL_TENANT
    assert resolve_tenant(None, account_id=PAPER_ACCOUNT, env="SIMULATE").tenant_id == PAPER_TENANT


def test_env_only_with_registered_account_conflict_still_raises():
    """账户存在但环境与声明不符 → 仍应抛（不是静默取一个）。"""
    with pytest.raises(ValueError):
        resolve_tenant(account_id=PAPER_ACCOUNT, env="REAL")


def test_alias_env_still_resolves():
    """别名形态（迁移脚本用）：``resolve_tenant("REAL")`` / ``"PAPER"``。"""
    assert resolve_tenant("REAL").tenant_id == REAL_TENANT
    assert resolve_tenant("PAPER").tenant_id == PAPER_TENANT


# --------------------------------------------------------------------------
# tenant_of_broker：写侧入口的行为边界
# --------------------------------------------------------------------------


def test_tenant_of_broker_resolves_by_account():
    """生产 broker 只有 account_id —— 必须能反解出租户。"""
    tenant, account = tenant_of_broker(_Broker(account_id=REAL_ACCOUNT))
    assert tenant == REAL_TENANT
    assert str(account) == REAL_ACCOUNT


def test_tenant_of_broker_strict_raises_on_unknown_account():
    with pytest.raises(Exception):  # TenantConfigKeyError
        tenant_of_broker(_Broker(account_id="999999999"), strict=True)


def test_tenant_of_broker_lenient_returns_none_not_default_tenant():
    """非 strict 模式下解析不出 → ``None``，**绝不**回落到某个默认租户。"""
    tenant, account = tenant_of_broker(_Broker(account_id="999999999"), strict=False)
    assert tenant is None
    assert str(account) == "999999999"
