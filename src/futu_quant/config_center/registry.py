"""统一配置注册表 (ConfigRegistry, Phase 0)。

实现只读配置目录与双向键解析：
- canonical key <-> aliases 双向映射；
- 显式来源与分类管理；
- 严格只读，不改写任何运行时解析逻辑；
- 供 CI 守卫与未来配置中心服务查询。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .spec import ConfigSpec

_DATA_PATH = Path(__file__).resolve().parent / "_specs_data.json"


class ConfigRegistry:
    """不可变统一配置注册表。"""

    def __init__(self, specs: list[ConfigSpec]):
        self._specs_by_key: dict[str, ConfigSpec] = {}
        self._alias_to_key: dict[str, str] = {}
        for spec in specs:
            if spec.key in self._specs_by_key:
                raise ValueError(f"Duplicate canonical key in registry: {spec.key}")
            self._specs_by_key[spec.key] = spec
            # 建立别名双向解析映射
            for alias in spec.aliases:
                alias_norm = alias.strip()
                if alias_norm in self._alias_to_key and self._alias_to_key[alias_norm] != spec.key:
                    raise ValueError(f"Conflicting alias '{alias_norm}' mapped to multiple keys: {self._alias_to_key[alias_norm]} vs {spec.key}")
                self._alias_to_key[alias_norm] = spec.key
            # canonical key 自身也作为 alias 保证完全幂等查询
            self._alias_to_key[spec.key] = spec.key

    def get(self, key_or_alias: str) -> ConfigSpec | None:
        """通过 canonical key 或历史 alias 查询配置规范定义。"""
        if not key_or_alias:
            return None
        norm = key_or_alias.strip()
        canonical = self._alias_to_key.get(norm)
        if canonical is not None:
            return self._specs_by_key.get(canonical)
        return self._specs_by_key.get(norm)

    def resolve_canonical_key(self, key_or_alias: str) -> str | None:
        """将任意历史 alias 或规范键解析为唯一 canonical key。"""
        if not key_or_alias:
            return None
        return self._alias_to_key.get(key_or_alias.strip())

    def contains(self, key_or_alias: str) -> bool:
        """检查配置项是否已在注册表登记。"""
        if not key_or_alias:
            return False
        return key_or_alias.strip() in self._alias_to_key

    def all_specs(self) -> list[ConfigSpec]:
        """返回所有已登记的配置项列表。"""
        return list(self._specs_by_key.values())

    def __iter__(self) -> Iterator[ConfigSpec]:
        return iter(self._specs_by_key.values())

    def __len__(self) -> int:
        return len(self._specs_by_key)


def _load_default_specs() -> list[ConfigSpec]:
    if not _DATA_PATH.exists():
        return []
    with open(_DATA_PATH, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    specs: list[ConfigSpec] = []
    for item in data:
        spec = ConfigSpec(
            key=item["key"],
            aliases=tuple(item.get("aliases", ())),
            category=item.get("category", "⑤ 策略总览"),
            title=item.get("title", item["key"]),
            description=item.get("description", ""),
            value_type=item.get("value_type", "string"),
            default=item.get("default"),
            scope=item.get("scope", "global"),
            storage=item.get("storage", "env"),
            risk_level=item.get("risk_level", "HIGH"),
            editable=bool(item.get("editable", True)),
            requires_admin=bool(item.get("requires_admin", False)),
            requires_confirmation=bool(item.get("requires_confirmation", False)),
            activation=item.get("activation", "next_cycle"),
            consumers=tuple(item.get("consumers", ())),
            tags=tuple(item.get("tags", ())),
            validator=item.get("validator", ""),
            dependencies=tuple(item.get("dependencies", ())),
            secret=bool(item.get("secret", False)),
            deprecated=bool(item.get("deprecated", False)),
        )
        specs.append(spec)
    return specs


GLOBAL_REGISTRY: ConfigRegistry = ConfigRegistry(_load_default_specs())
