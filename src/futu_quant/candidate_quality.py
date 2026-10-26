"""候选质量过滤（P0-5）：阈值单源 + 硬排除 + 软降权。

设计：``docs/candidate_quality_filter_design.md``。四条硬约束：

1. **阈值单源** —— 候选层与入场层读**同一个** ``resolve_candidate_quality(store, tenant_id)``，
   不各读各的；
2. **链式校验 fail-closed** —— ``soft_spread_pct ≤ hard_spread_pct ≤ entry_max_spread_pct``，
   任一违反 → **拒绝解析 + ERROR**。否则会出现「候选层放行、入场层拦住」的
   **不可解释 BLOCK**（配置错误路径重新引入）；
3. **租户显式入参** —— 由调用方给出或取 ``campaign["tenant_id"]``；
   **禁止从 broker 反解**（``risk.py`` 的三级反解是 fail-closed 红线，此处不得复用）；
4. **硬排除只作用于「候选列表」**，绝不「跳过 symbol」——
   已持仓的退出管理不受影响（A0 不变量，META 那次就是这么亏的）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import pandas as pd

from .data_quality import NEW_YORK, quote_freshness

# 排除/降权原因（`candidate_exclusions.reason`）
REASON_EXPIRED_T0 = "EXPIRED_T0"
REASON_QUOTE_STALE = "QUOTE_STALE"
REASON_BOOK_MISSING = "BOOK_MISSING"
REASON_SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
REASON_VOLUME_TOO_LOW = "VOLUME_TOO_LOW"

PHASE_HARD_EXCLUDE = "HARD_EXCLUDE"
PHASE_SOFT_DEMOTE = "SOFT_DEMOTE"
PHASE_ADMITTED = "ADMITTED"

CONFIG_KEY_TEMPLATE = "candidate_quality:{tenant_id}"

# 设计 §3：默认值 = 现值，行为不变
DEFAULTS: dict[str, Any] = {
    "hard_stale_seconds": 180,
    "soft_spread_pct": 10.0,          # 模拟盘默认；实盘由租户配置收紧到 5.0
    "soft_volume_min": 0,
    "entry_max_spread_pct": 12.0,     # = **真实入场门禁**（campaign `max_spread_pct` 的 VOLATILITY 默认值）
    "entry_max_stale_seconds": 300,   # 兜底上限（P0-5 §5.1 原 900 → 300）
    # ↑ 必须与下方 dataclass 默认值一致：`_coerce` 的 base 取自 dataclass，
    #   这里的值只是「已知键」集合的来源，写成别的值不会生效只会误导。
    #   2026-10-26 曾试调 600，解析链实际仍按 300，故收回维持 300（方向更严）。
    "hard_oi_min": 500,               # 既有字面量（earnings_gamma 调用点）
    "hard_spread_pct": 12.0,          # 既有字面量
    "hard_moneyness_max": 10.0,       # 既有字面量
}

REAL_SOFT_SPREAD_PCT = 5.0


class CandidateQualityError(RuntimeError):
    """阈值解析失败（缺失 / 非法 / 链式校验不过）。**fail-closed：不静默用默认值。**"""


@dataclass(frozen=True)
class CandidateQuality:
    hard_stale_seconds: int = 180
    soft_spread_pct: float = 10.0
    soft_volume_min: int = 0
    entry_max_spread_pct: float = 12.0
    entry_max_stale_seconds: int = 300
    hard_oi_min: int = 500
    hard_spread_pct: float = 12.0
    hard_moneyness_max: float = 10.0
    source: str = "default"
    tenant_id: str = ""

    def as_dict(self) -> dict:
        return {
            "hard_stale_seconds": self.hard_stale_seconds,
            "soft_spread_pct": self.soft_spread_pct,
            "soft_volume_min": self.soft_volume_min,
            "entry_max_spread_pct": self.entry_max_spread_pct,
            "entry_max_stale_seconds": self.entry_max_stale_seconds,
            "hard_oi_min": self.hard_oi_min,
            "hard_spread_pct": self.hard_spread_pct,
            "hard_moneyness_max": self.hard_moneyness_max,
            "source": self.source,
            "tenant_id": self.tenant_id,
        }


def validate_chain(config: CandidateQuality) -> None:
    """链式校验：``soft_spread_pct ≤ hard_spread_pct ≤ entry_max_spread_pct``。

    违反即抛 —— 这条防的是「候选层放行、入场层拦住」的**不可解释 BLOCK**：
    只校验「软 ≤ 入场」是不够的，租户仍可把硬筛设成 25%（> 入场 20%），
    BLOCK 会从「配置错误」这条路重新出现。
    """
    if not (config.soft_spread_pct <= config.hard_spread_pct <= config.entry_max_spread_pct):
        raise CandidateQualityError(
            "候选质量阈值链式校验失败：必须满足 "
            f"soft_spread_pct ≤ hard_spread_pct ≤ entry_max_spread_pct，"
            f"实际 {config.soft_spread_pct} / {config.hard_spread_pct} / "
            f"{config.entry_max_spread_pct}"
        )


def candidate_quality_key(tenant_id: str) -> str:
    """租户级配置键。**键形态唯一出处**，调用方不要手拼。"""
    return CONFIG_KEY_TEMPLATE.format(tenant_id=tenant_id or "")


def _coerce(raw: dict, base: CandidateQuality, source: str, tenant_id: str) -> CandidateQuality:
    """把配置字典叠到默认值上。**只接受已知键**，未知键忽略（不静默改语义）。"""
    known = set(DEFAULTS)
    values = {key: base.__dict__[key] for key in known}
    for key, value in (raw or {}).items():
        if key not in known or value is None:
            continue
        try:
            values[key] = int(value) if key in {
                "hard_stale_seconds", "soft_volume_min", "hard_oi_min",
                "entry_max_stale_seconds",
            } else float(value)
        except (TypeError, ValueError) as exc:
            raise CandidateQualityError(f"{source} 的 {key} 不是数字：{value!r}") from exc
    return replace(base, source=source, tenant_id=tenant_id, **values)


def resolve_candidate_quality(
    store: Any, tenant_id: str | None = None, *, is_real: bool = False,
) -> CandidateQuality:
    """三层解析链（与 P0-7 `resolve_risk_limits` 同构）：

    ① 账户级 ``real_account_config:{account_id}`` 的 ``candidate_quality`` 段（本批不启用，
       保留同构入口）→ ② 租户级 ``candidate_quality:{tenant_id}`` → ③ 默认值。

    **fail-closed**：配置存在但非法（非数字 / 链式校验不过）→ 抛 ``CandidateQualityError``，
    **不静默回落默认值** —— 静默回落会让「配置写了但没生效」变成不可解释状态。
    """
    tenant_id = str(tenant_id or "")
    base = replace(
        CandidateQuality(),
        soft_spread_pct=REAL_SOFT_SPREAD_PCT if is_real else DEFAULTS["soft_spread_pct"],
    )

    raw: dict = {}
    source = "default"
    if tenant_id:
        key = candidate_quality_key(tenant_id)
        text = ""
        try:
            text = store.get_setting(key, "") or ""
        except Exception:  # noqa: BLE001 - 读配置失败按「未配置」处理
            text = ""
        if text:
            import json

            try:
                parsed = json.loads(text)
            except (TypeError, ValueError) as exc:
                raise CandidateQualityError(f"{key} 不是合法 JSON：{exc}") from exc
            if not isinstance(parsed, dict):
                raise CandidateQualityError(f"{key} 必须是 JSON 对象，实际 {type(parsed).__name__}")
            raw, source = parsed, key

    config = _coerce(raw, base, source, tenant_id)
    validate_chain(config)
    return config


def resolve_candidate_quality_for_campaign(
    store: Any, campaign: dict, *, is_real: bool = False,
) -> CandidateQuality:
    """从 campaign 取租户（**显式入参优先，其次 campaign["tenant_id"]**）。

    **不从 broker 反解** —— 租户上下文必须由调用方显式给出（设计 §3 硬规则 4）。
    """
    tenant_id = str(campaign.get("tenant_id") or "")
    return resolve_candidate_quality(store, tenant_id, is_real=is_real)


def _spread_pct(row) -> float:
    value = row.get("spread_pct")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("inf")
    return number if pd.notna(number) else float("inf")


def _volume(row) -> float:
    for key in ("volume", "option_volume", "turnover"):
        if key in row:
            try:
                return float(row.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def filter_candidates(
    candidates: pd.DataFrame,
    config: CandidateQuality,
    *,
    now: datetime | None = None,
    tenant_id: str = "",
    strategy_id: str = "volatility_long_gamma",
    underlying: str = "",
) -> tuple[pd.DataFrame, list[dict]]:
    """硬排除 + 软降权。**只作用于候选列表，不触碰任何已持仓 campaign。**

    Returns:
        ``(保留的候选（含 soft_demoted 列）, 排除/降权留痕记录)``

    硬排除（不进候选池）：
    - ``QUOTE_STALE``     —— 报价停滞 > ``hard_stale_seconds``（**实测秒数**留痕）
    - ``BOOK_MISSING``    —— 报价没有可用更新时间（拿不到 `update_time`）
    - ``SPREAD_TOO_WIDE`` —— 价差 > ``hard_spread_pct``

    软降权（**仍在候选池**，只改排序）：价差 > ``soft_spread_pct`` 或量能 < ``soft_volume_min``。
    **软降权 ≠ 硬排除** —— 若实现成硬排除，5% 会砍掉大量合约，反而回到「没有候选」。
    """
    current = now or datetime.now(NEW_YORK)
    records: list[dict] = []
    if candidates is None or candidates.empty:
        result = candidates.copy() if candidates is not None else pd.DataFrame()
        if "soft_demoted" not in result.columns:
            result["soft_demoted"] = pd.Series(dtype=bool)
        return result, records

    keep_mask: list[bool] = []
    demoted: list[bool] = []

    for _, row in candidates.iterrows():
        code = str(row.get("code", ""))
        fresh, _reason, age = quote_freshness(row, current, config.hard_stale_seconds)
        spread = _spread_pct(row)
        volume = _volume(row)

        def _record(reason: str, phase: str) -> None:
            records.append({
                "tenant_id": tenant_id,
                "strategy_id": strategy_id,
                "underlying": underlying,
                "code": code,
                "reason": reason,
                "age_seconds": None if age == float("inf") else round(float(age), 1),
                "spread_pct": None if spread == float("inf") else round(spread, 4),
                "volume": volume,
                "phase": phase,
            })

        # ① 硬排除：报价可用性
        if not fresh:
            _record(
                REASON_BOOK_MISSING if age == float("inf") else REASON_QUOTE_STALE,
                PHASE_HARD_EXCLUDE,
            )
            keep_mask.append(False)
            demoted.append(False)
            continue
        # ② 硬排除：价差超过硬线
        if spread > config.hard_spread_pct:
            _record(REASON_SPREAD_TOO_WIDE, PHASE_HARD_EXCLUDE)
            keep_mask.append(False)
            demoted.append(False)
            continue

        # ③ 软降权：**不排除**，只标记，让主排序键把它排到后面
        is_demoted = spread > config.soft_spread_pct or volume < config.soft_volume_min
        if is_demoted:
            _record(
                REASON_SPREAD_TOO_WIDE if spread > config.soft_spread_pct
                else REASON_VOLUME_TOO_LOW,
                PHASE_SOFT_DEMOTE,
            )
        keep_mask.append(True)
        demoted.append(is_demoted)

    result = candidates.copy()
    result["soft_demoted"] = demoted
    result = result[keep_mask].reset_index(drop=True)
    return result, records
