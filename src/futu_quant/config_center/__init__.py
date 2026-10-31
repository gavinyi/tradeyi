"""统一配置中心模块导出。"""

from __future__ import annotations

from .spec import ConfigSpec
from .registry import ConfigRegistry, GLOBAL_REGISTRY, get_draftable_config_keys

__all__ = [
    "ConfigSpec",
    "ConfigRegistry",
    "GLOBAL_REGISTRY",
    "get_draftable_config_keys",
]
