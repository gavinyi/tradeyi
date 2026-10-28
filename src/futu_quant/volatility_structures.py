from __future__ import annotations

import hashlib

import pandas as pd

from .gamma_risk import market_number


STRUCTURE_LABELS = {
    "LONG_OPTION": "单腿买方期权",
    "CALL_DEBIT_SPREAD": "认购借方价差",
    "PUT_DEBIT_SPREAD": "认沽借方价差",
    "CALL_BACKSPREAD": "认购比例反向价差",
    "PUT_BACKSPREAD": "认沽比例反向价差",
}

# 入场 IV 历史百分位上限：campaign 可配，默认 0.75（与历史硬编码一致）。
# 边界 0.5–0.95；配错（缺失/非数字/越上界）一律 fail-closed 回落 0.75，
# 只允许「收紧」（<0.5 夹到 0.5），不允许因配错就放开闸门。
DEFAULT_IV_CAP = 0.75
IV_CAP_MIN = 0.5
IV_CAP_MAX = 0.95


def resolve_iv_percentile_cap(campaign: dict | None) -> float:
    """从 campaign 读入场 IV 百分位上限，非法回落默认 0.75（fail-closed）。

    - 缺失 / 非数字 → 0.75
    - < 0.5 → 夹到 0.5（只允许收紧，更安全）
    - > 0.95 → 回落 0.75（上限外太松，不应放行）
    """
    raw = (campaign or {}).get("maximum_entry_iv_percentile")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_IV_CAP
    if value < IV_CAP_MIN:
        return IV_CAP_MIN
    if value > IV_CAP_MAX:
        return DEFAULT_IV_CAP
    return value


def automatic_combo_id(strategy_id: str, campaign: dict, cycle: int = 0) -> str:
    identity = (
        f"{strategy_id}|{campaign.get('campaign_id', campaign.get('code', ''))}|"
        f"{campaign.get('event_date', '')}|{campaign.get('structure', '')}|{cycle}"
    )
    return "auto-" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def _leg(row: pd.Series, side: str, ratio: int, role: str) -> dict:
    return {
        "code": str(row["code"]), "side": side, "ratio": int(ratio),
        "role": role,
    }


def _package(
    structure: str,
    legs: list[tuple[pd.Series, str, int, str]],
    *,
    minimum_expected_return: float,
    premium_budget: float,
    iv_cap: float,
) -> dict[str, object]:
    entry_debit = 0.0
    exit_value = 0.0
    net_gamma = 0.0
    worst_spread = 0.0
    minimum_oi = float("inf")
    minimum_volume = float("inf")
    maximum_iv_percentile = 0.0
    iv_percentile_missing = False
    maximum_iv_rv = 0.0
    iv_rv_limit = float("inf")
    strikes: list[float] = []
    normalized_legs = []
    for row, side, ratio, role in legs:
        sign = 1 if side == "BUY" else -1
        entry_price = market_number(row.get("ask_price")) if sign > 0 else market_number(row.get("bid_price"))
        exit_price = market_number(row.get("expected_exit_bid")) if sign > 0 else market_number(row.get("expected_exit_ask"))
        entry_debit += sign * ratio * entry_price
        exit_value += sign * ratio * exit_price
        net_gamma += sign * ratio * abs(market_number(row.get("option_gamma")))
        worst_spread = max(worst_spread, market_number(row.get("spread_pct"), 100))
        minimum_oi = min(minimum_oi, market_number(row.get("option_open_interest")))
        minimum_volume = min(minimum_volume, market_number(row.get("volume")))
        raw_iv_pct = row.get("iv_history_percentile")
        if raw_iv_pct is None or (isinstance(raw_iv_pct, float) and pd.isna(raw_iv_pct)):
            # 记录「缺失」这件事本身：缺省值 1 只用于 fail-closed 判定，
            # 但原因文案必须说「缺失」，不能伪装成「100% 超限」误导排查。
            iv_percentile_missing = True
        maximum_iv_percentile = max(
            maximum_iv_percentile, market_number(raw_iv_pct, 1)
        )
        maximum_iv_rv = max(maximum_iv_rv, market_number(row.get("iv_rv"), 99))
        iv_rv_limit = min(iv_rv_limit, market_number(row.get("iv_rv_limit"), 1.5))
        strikes.append(market_number(row.get("strike_price")))
        normalized_legs.append(_leg(row, side, ratio, role))
    width = max(strikes) - min(strikes) if strikes else 0.0
    if "BACKSPREAD" in structure:
        risk_per_share = max(0.01, width + entry_debit)
    else:
        risk_per_share = max(0.01, entry_debit)
    expected_return = (exit_value - entry_debit) / risk_per_share
    max_loss = risk_per_share * 100
    # Preserve the prior configurable tier threshold; risk containment is handled
    # by max-loss budget and the combo rollback/naked-exposure guards.
    risk_reward_ok = expected_return >= float(minimum_expected_return)
    eligible = bool(
        entry_debit > 0
        and max_loss <= premium_budget
        and risk_reward_ok
        and net_gamma > 0
        and worst_spread <= 6.0
        and minimum_oi >= 800
        and minimum_volume >= 10
        and maximum_iv_percentile <= iv_cap
        and maximum_iv_rv <= iv_rv_limit
    )
    reasons = []
    if entry_debit <= 0:
        reasons.append("当前仅自动执行净借记结构")
    if max_loss > premium_budget:
        reasons.append(f"最大风险 ${max_loss:,.0f} 超过预算 ${premium_budget:,.0f}")
    if not risk_reward_ok:
        reasons.append(
            f"短周期净期望收益 {expected_return:.2%} 低于 {float(minimum_expected_return):.2%}"
        )
    if net_gamma <= 0:
        reasons.append("组合净 Gamma 不为正")
    if worst_spread > 6:
        reasons.append(f"最差腿价差 {worst_spread:.1f}% 超过 6%")
    if minimum_oi < 800:
        reasons.append(f"最小未平仓量 {minimum_oi:.0f} 低于 800")
    if minimum_volume < 10:
        reasons.append(f"最小成交量 {minimum_volume:.0f} 低于 10")
    if iv_percentile_missing:
        reasons.append(
            f"最高腿 IV 百分位缺失（证据不足，按 fail-closed 拦下，生效上限 {iv_cap:.0%}）"
        )
    elif maximum_iv_percentile > iv_cap:
        reasons.append(
            f"最高腿 IV 百分位 {maximum_iv_percentile:.0%} 超过 {iv_cap:.0%}"
        )
    if maximum_iv_rv > iv_rv_limit:
        reasons.append(f"最高腿 IV/RV {maximum_iv_rv:.2f} 超过 {iv_rv_limit:.2f}")
    primary = next(row for row, side, _, _ in legs if side == "BUY")
    return {
        "code": str(primary["code"]),
        "option_type": str(primary["option_type"]),
        "structure": structure,
        "structure_label": STRUCTURE_LABELS[structure],
        "legs": normalized_legs,
        "structure_net_debit": float(entry_debit),
        "structure_max_loss": float(max_loss),
        "structure_expected_return": float(expected_return),
        "model_expected_return": float(expected_return),
        "estimated_profit_probability": float(primary.get("estimated_profit_probability", 0)),
        "net_gamma": float(net_gamma),
        "spread_pct": float(worst_spread),
        "option_open_interest": float(minimum_oi),
        "volume": float(minimum_volume),
        "iv_history_percentile": float(maximum_iv_percentile),
        "iv_rv": float(maximum_iv_rv),
        "iv_rv_limit": float(iv_rv_limit),
        "volatility_eligible": eligible,
        "volatility_filter_reason": "；".join(reasons) or "全部结构条件通过",
        "volatility_quality_score": float(primary.get("volatility_quality_score", 0)),
        "primary_row": primary.to_dict(),
    }


def build_volatility_structure_candidates(
    candidates: pd.DataFrame,
    direction: str,
    *,
    minimum_expected_return: float,
    premium_budget: float,
    campaign: dict | None = None,
    store: object | None = None,
    strategy_id: str | None = None,
) -> pd.DataFrame:
    """Route a directional signal across single-leg and defined-risk expressions.

    ``campaign`` / ``store`` / ``strategy_id`` 用于入场 IV 百分位上限的
    campaign 级配置与审计留痕：生效值 > 默认 0.75 时写一条 ``audit_events``，
    让「放宽风控闸门」可追溯（谁、什么时候、放宽到多少）。
    UI 旧调用点不传这些参数 → 走默认 0.75、不审计。
    """
    iv_cap = resolve_iv_percentile_cap(campaign)
    # 放宽闸门必须留痕：生效值高于默认 0.75 才记，收紧（<=0.75）不记。
    if store is not None and iv_cap > DEFAULT_IV_CAP:
        try:
            store.audit(
                "VOLATILITY_IV_CAP_RELAXED",
                {
                    "tenant_id": str((campaign or {}).get("tenant_id") or ""),
                    "campaign_id": str(
                        (campaign or {}).get("campaign_id", (campaign or {}).get("code", ""))
                    ),
                    "effective_iv_cap": iv_cap,
                    "default_iv_cap": DEFAULT_IV_CAP,
                    "raw_value": (campaign or {}).get("maximum_entry_iv_percentile"),
                },
                strategy_id,
                actor="service:volatility",
            )
        except Exception:  # noqa: BLE001 - 留痕失败不阻断选结构
            pass
    if candidates.empty:
        return pd.DataFrame()
    direction = str(direction).upper()
    frame = candidates[
        candidates["option_type"].astype(str).str.upper() == direction
    ].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["abs_delta"] = pd.to_numeric(frame["option_delta"], errors="coerce").abs()
    frame["strike_numeric"] = pd.to_numeric(frame["strike_price"], errors="coerce")
    rows: list[dict[str, object]] = []

    for _, row in frame.iterrows():
        single = row.to_dict()
        single.update({
            "structure": "LONG_OPTION",
            "structure_label": STRUCTURE_LABELS["LONG_OPTION"],
            "legs": [_leg(row, "BUY", 1, "PROTECTIVE")],
            "structure_net_debit": market_number(row.get("ask_price")),
            "structure_max_loss": market_number(row.get("ask_price")) * 100,
            "structure_expected_return": market_number(row.get("model_expected_return"), -1),
            "primary_row": row.to_dict(),
        })
        rows.append(single)

    for expiry, expiry_frame in frame.groupby(frame["strike_time"].astype(str)):
        del expiry
        ordered = expiry_frame.sort_values("strike_numeric")
        near = ordered.iloc[(ordered["abs_delta"] - .55).abs().argmin()]
        if direction == "CALL":
            further = ordered[ordered["strike_numeric"] > float(near["strike_numeric"])]
        else:
            further = ordered[ordered["strike_numeric"] < float(near["strike_numeric"])]
        if further.empty:
            continue
        wing = further.iloc[(further["abs_delta"] - .32).abs().argmin()]
        rows.append(_package(
            f"{direction}_DEBIT_SPREAD",
            [(near, "BUY", 1, "PROTECTIVE"), (wing, "SELL", 1, "RISK")],
            minimum_expected_return=minimum_expected_return,
            premium_budget=premium_budget,
            iv_cap=iv_cap,
        ))
        rows.append(_package(
            f"{direction}_BACKSPREAD",
            [(near, "SELL", 1, "RISK"), (wing, "BUY", 2, "PROTECTIVE")],
            minimum_expected_return=minimum_expected_return,
            premium_budget=premium_budget,
            iv_cap=iv_cap,
        ))
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    return result.sort_values(
        ["volatility_eligible", "structure_expected_return", "volatility_quality_score"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
