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
    validator: str = ""              # 命名校验器
    dependencies: tuple[str, ...] = () # 依赖项
    secret: bool = False             # 是否涉密
    deprecated: bool = False         # 是否已弃用
