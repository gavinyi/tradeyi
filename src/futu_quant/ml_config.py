"""ML 打分方式的**租户级开关**（P2-3 开关部分）。

现状问题（2026-10-24 核实）：

- 打分开关只有全局 env `ML_ENABLED`，**没有租户级配置**，也没有页面开关；
- 两处默认值不一致：`ml_symbol_scorer.py:54` 缺省 `true`（启用），
  `config.py:804` 缺省 `false`（禁用）—— 两套默认迟早互相打脸；
- 于是「只想在模拟盘先试 ML、实盘继续规则」这件事做不到，只能一开全开。

本模块给出**唯一口径**：

    scoring_mode:{tenant}  →  scoring_mode（全局）  →  默认 "rule"

- `ml`   = 使用 ML 打分（模型缺失/反序列化失败 → 降级为规则，属故障，须告警）
- `rule` = 显式使用规则打分（这是**配置选择**，不是故障，不告警）

**默认 `rule`**（用户 2026-10-24 确认：装依赖与开启 ML 放在最后，先保证功能完整）：
不显式开启就不上 ML —— 保守方向，避免"部署即变策略"。

历史 env `ML_ENABLED` 不再决定打分方式（只作为迁移提示），否则「默认 rule」会被
生产 `.env` 里遗留的 `ML_ENABLED=true` 静默推翻。
"""

from __future__ import annotations

from .store import TradingStore
from .tenant import TenantConfigKeyError, resolve_tenant_config_key

SCORING_MODE_BASE = "scoring_mode"
MODE_ML = "ml"
MODE_RULE = "rule"
VALID_MODES = (MODE_ML, MODE_RULE)
DEFAULT_MODE = MODE_RULE

MODE_LABELS = {
    MODE_ML: "ML 打分",
    MODE_RULE: "规则打分（显式选择）",
}


def scoring_mode_key(tenant_id: str | None) -> str | None:
    """租户键；租户缺失/未注册返回 None（调用方回落全局键）。"""
    tenant = str(tenant_id or "").strip()
    if not tenant:
        return None
    try:
        return resolve_tenant_config_key(tenant, SCORING_MODE_BASE)
    except TenantConfigKeyError:
        return None


def resolve_scoring_mode(store: TradingStore, tenant_id: str | None) -> str:
    """解析生效的打分方式：租户键 → 全局键 → `rule`。

    非法值**不静默兜底成 ML**：落 ERROR 事件并按 `rule` 处理（失败安全方向）。
    """
    key = scoring_mode_key(tenant_id)
    raw = ""
    source = "default"
    if key:
        raw = str(store.get_setting(key, "") or "").strip().lower()
        if raw:
            source = f"tenant:{tenant_id}"
    if not raw:
        raw = str(store.get_setting(SCORING_MODE_BASE, "") or "").strip().lower()
        if raw:
            source = "global"
    if not raw:
        return DEFAULT_MODE
    if raw not in VALID_MODES:
        store.operational_event(
            "ml_config", "SCORING_MODE_INVALID", "ERROR",
            {
                "tenant_id": str(tenant_id or ""),
                "source": source,
                "value": raw,
                "valid": list(VALID_MODES),
                "fallback": DEFAULT_MODE,
            },
        )
        return DEFAULT_MODE
    return raw


def set_scoring_mode(store: TradingStore, tenant_id: str | None, mode: str) -> str:
    """写租户级开关（只写租户键）；返回写入的键。

    拿不到租户时**拒绝写**：写全局键会让所有租户一起切换打分方式。
    """
    tenant = str(tenant_id or "").strip()
    if not tenant:
        raise ValueError("无法解析当前租户，拒绝写全局打分开关（会连带切换其他租户）")
    value = str(mode or "").strip().lower()
    if value not in VALID_MODES:
        raise ValueError(f"非法打分方式 {mode!r}，只允许 {list(VALID_MODES)}")
    key = resolve_tenant_config_key(tenant, SCORING_MODE_BASE)
    store.set_setting(key, value)
    return key

# ---------------------------------------------------------------------------
# 其余 ML 因子（PRD §2.1 的 9 项里，除 scoring_mode 外可运行时配置的那些）
#
# 统一走「租户键 → 全局键 → 默认」三层解析，**非法值落 ERROR 并按默认值处理**：
# 配置写错不应表现为「ML 悄悄换了阈值」，而应表现为「一条 ERROR + 仍按安全默认跑」。
# ---------------------------------------------------------------------------
_SPEC_FLOAT = "float"
_SPEC_INT = "int"
_SPEC_STR = "str"

# name: (kind, min, max, default, label)
FACTOR_SPECS: dict[str, dict] = {
    "ml_threshold": {"kind": _SPEC_FLOAT, "min": 0.0, "max": 1.0, "default": 0.5,
                     "label": "打分阈值（高于它才算入选）"},
    "ml_weight": {"kind": _SPEC_FLOAT, "min": 0.0, "max": 2.0, "default": 0.6,
                  "label": "ML 对排序的影响权重"},
    "min_samples": {"kind": _SPEC_INT, "min": 10, "max": 1_000_000, "default": 200,
                    "label": "训练最少样本数（不足则 SKIP）"},
    "train_window_days": {"kind": _SPEC_INT, "min": 7, "max": 3650, "default": 180,
                          "label": "训练窗口（天）"},
    "auc_guard": {"kind": _SPEC_FLOAT, "min": 0.0, "max": 1.0, "default": 0.50,
                  "label": "AUC 安全门槛（低于它不替换生产模型）"},
    "min_improvement_margin": {"kind": _SPEC_FLOAT, "min": 0.0, "max": 0.5, "default": 0.01,
                               "label": "替换所需的相对改进边际"},
    "model_path": {"kind": _SPEC_STR, "min": None, "max": None,
                   "default": "data/ml_models/volatility_scorer_v3.pkl",
                   "label": "模型文件路径"},
}


def _coerce(kind: str, raw: str):
    if kind == _SPEC_INT:
        return int(float(raw))
    if kind == _SPEC_FLOAT:
        return float(raw)
    return str(raw)


def _valid(spec: dict, value) -> bool:
    if spec["kind"] == _SPEC_STR:
        return bool(str(value or "").strip())
    if spec["min"] is not None and value < spec["min"]:
        return False
    if spec["max"] is not None and value > spec["max"]:
        return False
    return True


def resolve_ml_factor(store: TradingStore, tenant_id: str | None, name: str):
    """三层解析一个 ML 因子；非法值落 ERROR 并按默认值处理。"""
    if name not in FACTOR_SPECS:
        raise KeyError(f"未知 ML 因子：{name}")
    spec = FACTOR_SPECS[name]
    key = scoring_mode_key(tenant_id)  # 复用同一套租户键校验（返回 None 表示不可用）
    candidates: list[tuple[str, str]] = []
    if key:
        tenant_key = key.replace(SCORING_MODE_BASE, name, 1)
        candidates.append((tenant_key, f"tenant:{tenant_id}"))
    candidates.append((name, "global"))
    for config_key, source in candidates:
        raw = str(store.get_setting(config_key, "") or "").strip()
        if not raw:
            continue
        try:
            value = _coerce(spec["kind"], raw)
        except (TypeError, ValueError):
            value = None
        if value is not None and _valid(spec, value):
            return value
        store.operational_event(
            "ml_config", "ML_FACTOR_INVALID", "ERROR",
            {
                "factor": name, "tenant_id": str(tenant_id or ""), "source": source,
                "value": raw, "fallback": spec["default"],
                "rule": f"kind={spec['kind']} range=[{spec['min']}, {spec['max']}]",
            },
        )
        break
    return spec["default"]


def set_ml_factor(store: TradingStore, tenant_id: str | None, name: str, value) -> str:
    """写租户级 ML 因子（只写租户键）；非法值/无租户一律拒绝。"""
    if name not in FACTOR_SPECS:
        raise KeyError(f"未知 ML 因子：{name}")
    tenant = str(tenant_id or "").strip()
    if not tenant:
        raise ValueError("无法解析当前租户，拒绝写全局 ML 配置（会连带影响其他租户）")
    spec = FACTOR_SPECS[name]
    coerced = _coerce(spec["kind"], str(value))
    if not _valid(spec, coerced):
        raise ValueError(f"非法值 {value!r}：因子 {name} 要求 {spec['kind']} 在 [{spec['min']}, {spec['max']}]")
    key = resolve_tenant_config_key(tenant, name)
    store.set_setting(key, str(coerced))
    return key
