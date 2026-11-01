"""统一配置项定义与不可变元数据模型 (Phase 0)。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConfigSpec:
    """统一配置项不可变元数据标准模型 (Phase 0 只读目录)。

    严格描述配置项的规范键名、历史别名、分类、类型、存储层、作用域、
    生效方式、风险等级及安全控制属性，不承载任何运行时写入逻辑。
    """
    key: str                         # 稳定 canonical key，分层命名 (如 runtime.execution.mode)
    aliases: tuple[str, ...]         # 历史别名 (如 EXECUTION_MODE, execution_mode)
    category: str                    # 一级业务分类
    title: str                       # 中文名称
    description: str                 # 业务说明与解释
    value_type: str                  # bool/int/float/string/enum/symbols/json/secret_ref
    default: Any                     # 代码默认值
    scope: str                       # global/tenant/account/strategy/symbol
    storage: str                     # runtime_config/system_settings/env/derived
    risk_level: str                  # LOW/MEDIUM/HIGH/CRITICAL
    editable: bool                   # 是否允许用户编辑 (Phase 0 统一定义)
    requires_admin: bool             # 是否需要管理员权限
    requires_confirmation: bool      # 是否需要二次确认 / 短时确认会话
    activation: str                  # immediate/next_cycle/restart/manual_release
    consumers: tuple[str, ...]       # 消费者模块、服务或入口
    tags: tuple[str, ...]            # 标签检索视图 (如 real_safety, risk, options)
    runtime_key: str                 # 真实落库键名 (Phase 2 显式化，必须属于 aliases 且全局唯一)
    user_area: str                   # 业务入口归属 (PRD: strategy_center / risk_limits / account_env / ops_notify，必填无默认)
    surface: str                     # 展示层级 (PRD: primary / advanced，必填无默认，primary限制首屏<=30)
    validator: str = ""              # 命名校验器
    dependencies: tuple[str, ...] = () # 依赖项
    secret: bool = False             # 是否涉密
    deprecated: bool = False         # 是否已弃用

    def __post_init__(self) -> None:
        valid_areas = {"strategy_center", "risk_limits", "account_env", "ops_notify"}
        if not self.user_area or self.user_area not in valid_areas:
            raise ValueError(
                f"ConfigSpec {self.key} 的 user_area 非法或缺失: {self.user_area!r}！"
                f"必须为 {sorted(valid_areas)} 之一，严禁空值或未分类！"
            )
        valid_surfaces = {"primary", "advanced"}
        if not self.surface or self.surface not in valid_surfaces:
            raise ValueError(
                f"ConfigSpec {self.key} 的 surface 非法或缺失: {self.surface!r}！"
                f"必须为 {sorted(valid_surfaces)} 之一，严禁空值！"
            )

