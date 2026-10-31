"""统一配置中心模块导出。"""

from __future__ import annotations

from .spec import ConfigSpec
from .registry import ConfigRegistry, GLOBAL_REGISTRY, get_draftable_config_keys, validate_instance_key
from .service import BATCH_3_ALLOWED_EDIT_KEYS, edit_low_risk_config, rollback_low_risk_config

__all__ = [
    "ConfigSpec",
    "ConfigRegistry",
    "GLOBAL_REGISTRY",
    "get_draftable_config_keys",
    "validate_instance_key",
    "BATCH_3_ALLOWED_EDIT_KEYS",
    "edit_low_risk_config",
    "rollback_low_risk_config",
]
