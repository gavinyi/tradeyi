"""
通用多租户元数据模型与租户注册表 (N-Tenant Architecture)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TenantProfile:
    """全局租户元数据契约模型"""
    tenant_id: str                      # 全局唯一租户主键，例如 "futu_real_master", "futu_paper_sandbox"
    display_name: str                   # 界面与日志可读名称，如 "富途·实盘真金主专户"
    broker_type: str                    # 底座券商："futu", "ibkr", "alpaca" 等
    trading_env: Literal["REAL", "SIMULATE"]  # 真实资金环境还是沙盒环境
    account_id: str                     # 券商层真实账号（如 "281756481476169109", "20170229"）
    gateway_host: str = "127.0.0.1"     # 网关地址
    gateway_port: int = 11111           # 网关端口
    risk_profile: str = "default"       # 绑定的风控配置文件或规则 ID
    is_active: bool = True              # 租户启用状态


DEFAULT_TENANTS: dict[str, TenantProfile] = {
    "futu_real_master": TenantProfile(
        tenant_id="futu_real_master",
        display_name="富途·实盘真金主专户",
        broker_type="futu",
        trading_env="REAL",
        account_id="281756481476169109",
        gateway_host="127.0.0.1",
        gateway_port=11111,
        risk_profile="real_strict",
        is_active=True,
    ),
    "futu_paper_sandbox": TenantProfile(
        tenant_id="futu_paper_sandbox",
        display_name="富途·模拟沙盒专户",
        broker_type="futu",
        trading_env="SIMULATE",
        account_id="20170229",
        gateway_host="127.0.0.1",
        gateway_port=11111,
        risk_profile="paper_sandbox",
        is_active=True,
    ),
    "moomoo_real": TenantProfile(
        tenant_id="moomoo_real",
        display_name="Moomoo·US备用专户",
        broker_type="futu",
        trading_env="REAL",
        account_id="283445327916889204",
        gateway_host="127.0.0.1",
        gateway_port=11111,
        risk_profile="real_moomoo_standby",
        is_active=False,
    ),
}


def resolve_tenant(
    target: str | TenantProfile | None = None,
    *,
    account_id: str | int | None = None,
    env: str | None = None,
    registry: dict[str, TenantProfile] | None = None,
    strict: bool = False,
) -> TenantProfile | None:
    """
    根据 tenant_id / TenantProfile 对象或 (account_id, env) 解析租户配置。
    支持注入自定义 registry 供测试使用，避免全局依赖。
    """
    reg = registry if registry is not None else DEFAULT_TENANTS

    if isinstance(target, TenantProfile):
        return target

    if isinstance(target, str) and target.strip():
        tid = target.strip()
        if tid in reg:
            return reg[tid]
        # 支持以别名 "REAL" / "SIMULATE" / "PAPER" 解析默认租户
        upper_tid = tid.upper()
        if upper_tid == "REAL" and "futu_real_master" in reg:
            return reg["futu_real_master"]
        if upper_tid in {"SIMULATE", "PAPER"} and "futu_paper_sandbox" in reg:
            return reg["futu_paper_sandbox"]
        if strict:
            raise ValueError(f"未注册的租户标识: '{tid}'")

    acc_str = str(account_id).strip() if account_id is not None else ""
    env_str = str(env).upper().strip() if env is not None else ""

    # 精确匹配 account_id 与 env
    for profile in reg.values():
        if acc_str and profile.account_id == acc_str:
            if not env_str or profile.trading_env == env_str:
                return profile
        elif env_str and profile.trading_env == env_str and not acc_str:
            return profile

    if strict or acc_str:
        raise ValueError(f"无法依据输入解析合法租户: target={target}, account_id={account_id}, env={env}")

    # 仅在未给定具体账号时做向下兼容旧系统的环境级缺省
    if env_str == "REAL":
        return reg.get("futu_real_master")
    return reg.get("futu_paper_sandbox")
