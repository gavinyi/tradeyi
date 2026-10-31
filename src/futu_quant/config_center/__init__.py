"""统一配置中心模块导出。"""

from __future__ import annotations

from .spec import ConfigSpec
from .registry import ConfigRegistry, GLOBAL_REGISTRY, get_draftable_config_keys, validate_instance_key

__all__ = [
    "ConfigSpec",
    "ConfigRegistry",
    "GLOBAL_REGISTRY",
    "get_draftable_config_keys",
    "validate_instance_key",
]
