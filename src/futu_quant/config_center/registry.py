"""统一配置注册表 (ConfigRegistry, Phase 0)。

实现只读配置目录与双向键解析：
- canonical key <-> aliases 双向映射；
- 显式来源与分类管理；
- 严格只读，不改写任何运行时解析逻辑；
- 供 CI 守卫与未来配置中心服务查询。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .spec import ConfigSpec

_DATA_PATH = Path(__file__).resolve().parent / "_specs_data.json"


@dataclass(frozen=True)
class _TemplateEntry:
    prefix: str
    segment_count: int
    placeholders: tuple[str, ...]
    canonical_key: str
    template_str: str


class ConfigRegistry:
    """不可变统一配置注册表。"""

    def __init__(self, specs: list[ConfigSpec]):
        self._specs_by_key: dict[str, ConfigSpec] = {}
        self._alias_to_key: dict[str, str] = {}
        self._runtime_key_to_spec: dict[str, ConfigSpec] = {}
        self._template_index: dict[str, dict[int, _TemplateEntry]] = {}

        for spec in specs:
            if spec.key in self._specs_by_key:
                raise ValueError(f"Duplicate canonical key in registry: {spec.key}")
            self._specs_by_key[spec.key] = spec

            # Phase 2/3 强制约束：runtime_key 必须非空、必须属于自身的 aliases、且全局唯一
            if not spec.runtime_key:
                raise ValueError(f"配置项 {spec.key} 的 runtime_key 不能为空！")
            if spec.runtime_key not in spec.aliases:
                raise ValueError(
                    f"配置项 {spec.key} 的 runtime_key '{spec.runtime_key}' 必须属于其已声明的 aliases: {spec.aliases}！"
                )
            if spec.runtime_key in self._runtime_key_to_spec:
                existing = self._runtime_key_to_spec[spec.runtime_key]
                raise ValueError(
                    f"重复的 runtime_key '{spec.runtime_key}'，在 {existing.key} 与 {spec.key} 之间冲突！全局必须唯一！"
                )
            self._runtime_key_to_spec[spec.runtime_key] = spec

            # 建立别名双向解析映射与冒号切段前缀索引
            for alias in spec.aliases:
                alias_norm = alias.strip()
                if "{" in alias_norm and "}" in alias_norm:
                    # 动态变量键模板（按冒号切段 + 段数索引，免正则与空格限制）
                    parts = alias_norm.split(":")
                    prefix = parts[0]
                    seg_count = len(parts)
                    placeholders = tuple(p.strip("{}") for p in parts[1:])

                    if prefix not in self._template_index:
                        self._template_index[prefix] = {}
                    if seg_count in self._template_index[prefix]:
                        existing = self._template_index[prefix][seg_count]
                        raise ValueError(
                            f"模板冲突：前缀 '{prefix}' 段数 {seg_count} 已存在模板 '{existing.template_str}'，"
                            f"与新模板 '{alias_norm}' 冲突！单键至多命中唯一模板！"
                        )
                    self._template_index[prefix][seg_count] = _TemplateEntry(
                        prefix=prefix,
                        segment_count=seg_count,
                        placeholders=placeholders,
                        canonical_key=spec.key,
                        template_str=alias_norm,
                    )
                    # 模板字符串自身也注册为 alias，支持字面量优先秒级解析与元数据反射
                    self._alias_to_key[alias_norm] = spec.key
                else:
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
        canonical = self.resolve_canonical_key(norm)
        if canonical is not None:
            return self._specs_by_key.get(canonical)
        return self._specs_by_key.get(norm)

    def resolve_canonical_key(self, key_or_alias: str) -> str | None:
        """将任意历史 alias、变量实例键或规范键解析为唯一 canonical key。

        解析原则：
        1. 优先查字面量完全匹配（含模板串自身，如 risk_limits:{tenant}），秒级命中；
        2. 若字面量未命中，走冒号切段索引匹配：
           - 任何包含空段（如 'a::b' 或 'a:'）、前缀未索引、段数不匹配者，显式返回 None 未命中；
           - 精确匹配 prefix 与 segment_count，单键至多命中唯一个模板，杜绝正则歧义与空格限制。
        """
        if not key_or_alias:
            return None
        norm = key_or_alias.strip()
        # 1. 优先字面量完全匹配
        if norm in self._alias_to_key:
            return self._alias_to_key[norm]

        # 2. 冒号切段索引匹配
        parts = norm.split(":")
        if any(len(p.strip()) == 0 for p in parts):
            return None  # 空段或非法冒号格式，显式拒识

        prefix = parts[0]
        seg_count = len(parts)
        if prefix in self._template_index and seg_count in self._template_index[prefix]:
            return self._template_index[prefix][seg_count].canonical_key

        return None

    def contains(self, key_or_alias: str) -> bool:
        """检查配置项（含具体实例键）是否已在注册表登记。"""
        if not key_or_alias:
            return False
        return self.resolve_canonical_key(key_or_alias) is not None

    def all_specs(self) -> list[ConfigSpec]:
        """返回所有已登记的配置项列表。"""
        return list(self._specs_by_key.values())

    def __iter__(self) -> Iterator[ConfigSpec]:
        return iter(self._specs_by_key.values())

    def __len__(self) -> int:
        return len(self._specs_by_key)


def validate_instance_key(
    key: str,
    allowed_tenants: set[str] | list[str] | None = None,
    allowed_accounts: set[str] | list[str] | None = None,
    allowed_strategies: set[str] | list[str] | None = None,
    registry: ConfigRegistry | None = None,
) -> None:
    """在写入边界 (如 create_config_release) 严格校验配置项/实例键的合法性。

    门禁规则：
    1. 必须能在注册表中识别 (contains is True)；
    2. 检查 deprecated 状态：若对应 spec 已废弃，一律抛出 ValueError 阻断新建草稿；
    3. 若匹配切段模板：
       - 若包含 tenant 占位符：后缀租户必须属于 allowed_tenants 白名单（DEFAULT_TENANTS 与动态租户表并集）；
       - 若包含 account 占位符：后缀账户必须属于 allowed_accounts 白名单（严格纯数字券商 account_id 并集，严禁租户名冒充）；
       - 若包含 strategy 占位符：后缀策略必须属于 allowed_strategies 白名单（strategy_id 集合及存量 Title 别名，并支持动态审批策略）；
    不合法一律抛出 ValueError fail-closed 拒写。
    """
    reg = registry or GLOBAL_REGISTRY
    norm = key.strip() if key else ""
    if not norm or not reg.contains(norm):
        raise ValueError(f"配置键 '{key}' 未在统一配置中心登记，拒绝创建发布草稿")

    canonical_key = reg.resolve_canonical_key(norm)
    spec = reg.get(canonical_key)
    if spec and spec.deprecated:
        raise ValueError(f"配置项 '{key}' (规范键: {spec.key}) 已废弃 (deprecated)，禁止创建发布草稿！")

    parts = norm.split(":")
    prefix = parts[0]
    seg_count = len(parts)
    if prefix in reg._template_index and seg_count in reg._template_index[prefix]:
        template = reg._template_index[prefix][seg_count]
        var_values = {var: parts[i + 1].strip() for i, var in enumerate(template.placeholders)}

        # 1. 租户白名单校验
        if "tenant" in var_values:
            tenant_val = var_values["tenant"]
            from futu_quant.tenant import DEFAULT_TENANTS
            tenants = set(DEFAULT_TENANTS.keys())
            if allowed_tenants:
                tenants |= set(allowed_tenants)
            if tenant_val not in tenants:
                raise ValueError(
                    f"配置实例键 '{key}' 中的租户 '{tenant_val}' 未在有效租户注册表中登记！"
                    f"有效租户白名单: {sorted(tenants)}"
                )

        # 2. 账户白名单校验 (严格真实券商 account_id 并集)
        if "account" in var_values:
            account_val = var_values["account"]
            from futu_quant.tenant import DEFAULT_TENANTS
            known_accounts = {t.account_id for t in DEFAULT_TENANTS.values() if getattr(t, "account_id", None)}
            if allowed_accounts:
                known_accounts |= set(allowed_accounts)
            if account_val not in known_accounts:
                raise ValueError(
                    f"配置实例键 '{key}' 中的账户 '{account_val}' 未在有效账户注册表中登记！"
                    f"有效账户白名单: {sorted(known_accounts)}"
                )

        # 3. 策略白名单校验 (strategy_id 集合 + 现网 3 个存量 Title 别名)
        if "strategy" in var_values:
            strategy_val = var_values["strategy"]
            from futu_quant.strategy_plugins import PLUGINS
            valid_strategies = {p.strategy_id for p in PLUGINS}
            # 兼容现网 system_settings 存量的 3 个 Title 别名
            valid_strategies |= {"20 Day Breakout", "RSI Mean Reversion", "SMA Cross"}
            if allowed_strategies:
                valid_strategies |= set(allowed_strategies)
            if strategy_val not in valid_strategies:
                raise ValueError(
                    f"配置实例键 '{key}' 中的策略 '{strategy_val}' 未在有效策略插件注册表中登记！"
                    f"有效策略白名单: {sorted(valid_strategies)}"
                )


def _load_default_specs() -> list[ConfigSpec]:
    if not _DATA_PATH.exists():
        raise FileNotFoundError(
            f"统一配置注册表元数据文件缺失: {_DATA_PATH}！"
            "打包安装或环境部署必须包含 config_center/_specs_data.json，严禁静默空表启动！"
        )
    with open(_DATA_PATH, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not data:
        raise ValueError(f"统一配置注册表数据为空: {_DATA_PATH}，系统拒绝以空注册表运行！")
    specs: list[ConfigSpec] = []
    for item in data:
        aliases = tuple(item.get("aliases", ()))
        runtime_key = item.get("runtime_key")
        if not runtime_key:
            raise ValueError(f"配置项 {item.get('key')} 缺失必填字段 runtime_key！严禁任何默认回退或静默推导！")
        spec = ConfigSpec(
            key=item["key"],
            aliases=aliases,
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
            runtime_key=runtime_key,
        )
        specs.append(spec)
    return specs


GLOBAL_REGISTRY: ConfigRegistry = ConfigRegistry(_load_default_specs())


def get_draftable_config_keys(registry: ConfigRegistry | None = None) -> list[str]:
    """返回允许通过 UI/运维草稿发布链路编辑的配置键列表。

    Phase 2 规范化：直接读取 s.runtime_key 落库键，不再遍历 alias 列表。
    安全门禁：
    1. 必须存储于 runtime_config (storage == "runtime_config")；
    2. 明确标记为可编辑 (editable is True)；
    3. 严禁包含 CRITICAL 极高风险配置 (risk_level != "CRITICAL")；
    4. 严禁包含需要二次确认的敏感配置 (requires_confirmation is False)；
    5. 排除带模板变量占位的抽象键；
    6. 默认推荐配置置顶，其余按字典序稳定排序。
    """
    reg = registry or GLOBAL_REGISTRY
    candidate_keys: list[str] = []
    for s in reg.all_specs():
        if s.storage != "runtime_config" or not s.editable:
            continue
        if s.risk_level == "CRITICAL" or s.requires_confirmation:
            continue
        rk = s.runtime_key
        if "{" in rk and "}" in rk:
            continue
        if rk not in candidate_keys:
            candidate_keys.append(rk)

    preferred = "CORE_SATELLITE_DAILY_VALIDATION"
    candidate_keys.sort()
    if preferred in candidate_keys:
        candidate_keys.remove(preferred)
        candidate_keys.insert(0, preferred)
    return candidate_keys

