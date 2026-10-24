from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from .candidate_quality import (
    filter_candidates, resolve_candidate_quality_for_campaign,
)
from .earnings_gamma import screen_gamma_contracts
from .gamma_risk import contract_analysis, market_number, realized_volatility
from .option_lifecycle import parse_option_code
from .option_expectancy import add_iv_history_percentiles
from .option_ranking import rank_long_option_candidates
from .option_signal import automatic_option_direction
from .volatility_optimizer import (
    rank_volatility_contracts, tiered_premium_budget, volatility_regime,
    volatility_threshold_profile, volatility_min_expected_return,
)
from .volatility_structures import build_volatility_structure_candidates


MAINTAIN_SCORE_IMPROVEMENT = 8.0


def _roll_diagnostics(candidates: pd.DataFrame, regime: dict, message: str) -> dict:
    top = []
    for _, row in candidates.head(5).iterrows():
        entry = {
            "code": str(row.get("code", "")),
            "eligible": bool(row.get("volatility_eligible", False)),
            "reason": str(row.get("volatility_filter_reason", "")),
            "score": float(row.get("volatility_quality_score", 0) or 0),
            "iv_rv": float(row.get("iv_rv", 0) or 0),
            "iv_rv_limit": float(row.get("iv_rv_limit", 0) or 0),
            "premium": float(row.get("premium_per_stage", 0) or 0),
            "expected_return": float(row.get("model_expected_return", 0) or 0),
        }
        if "ml_score" in row and pd.notna(row.get("ml_score")):
            entry["ml_score"] = float(row["ml_score"])
        if "ml_rank" in row and pd.notna(row.get("ml_rank")):
            entry["ml_rank"] = str(row["ml_rank"])
        if "ml_adjusted_score" in row and pd.notna(row.get("ml_adjusted_score")):
            entry["ml_adjusted_score"] = float(row["ml_adjusted_score"])
        top.append(entry)
    return {
        "message": message,
        "risk_tier": (
            "强波动放宽" if regime.get("strong_signal_override")
            else "动态波动放宽" if regime.get("active") else "常规"
        ),
        "candidate_count": int(len(candidates)),
        "eligible_count": int(candidates.get(
            "volatility_eligible", pd.Series(dtype=bool)
        ).sum()),
        "top_candidates": top,
    }


def _with_diagnostics(campaign: dict, candidates: pd.DataFrame, regime: dict, message: str):
    result = dict(campaign)
    result["_roll_diagnostics"] = _roll_diagnostics(candidates, regime, message)
    return result, message


def should_replace_campaign(current: dict, candidate: dict) -> tuple[bool, str]:
    """Decide whether an empty volatility activity should track a better contract."""
    current_code = str(current.get("code", ""))
    candidate_code = str(candidate.get("code", ""))
    if not candidate_code or candidate_code == current_code:
        return False, "当前合约仍是本轮最佳表达"
    current_type = str(current.get("option_type", ""))
    candidate_type = str(candidate.get("option_type", ""))
    if current_type and candidate_type and current_type != candidate_type:
        return True, f"方向表达从 {current_type} 切换为 {candidate_type}"
    diagnostics = candidate.get("_roll_diagnostics") or {}
    top = diagnostics.get("top_candidates") or []
    if top:
        best = top[0]
        if not bool(best.get("eligible", False)):
            return False, "最高分候选仍不合格，不替换当前活动"
        best_score = float(best.get("score", 0) or 0)
        current_score = float(current.get("volatility_quality_score", 0) or 0)
        current_reason = str(current.get("volatility_filter_reason", ""))
        if current_reason and current_reason != "全部条件通过":
            return True, f"当前合约质量变差：{current_reason}"
        if best_score >= current_score + MAINTAIN_SCORE_IMPROVEMENT:
            return True, (
                f"新候选质量分 {best_score:.1f} 明显高于当前 "
                f"{current_score:.1f}"
            )
    return True, "动态波动仍有效，当前最佳候选已变化"


def roll_campaign(broker, campaign: dict, store) -> tuple[dict, str]:
    underlying = campaign["underlying"]
    # 活动策略不能继续持有或复核已到期合约；到期合约必须滚动到未来期限。
    # 到期判定使用纽约交易所时区，避免本地时区偏差导致误判。
    current_code = str(campaign.get("code", ""))
    current_contract = parse_option_code(current_code) if current_code else None
    ny_today = datetime.now(ZoneInfo("America/New_York")).date()
    if current_contract is not None and current_contract.expiration < ny_today:
        campaign = dict(campaign)
        campaign["code"] = ""
        campaign["status"] = "EXPIRED"
        campaign["last_expired_code"] = current_code
    owner = broker.snapshots([underlying]).iloc[0]
    bars = broker.candles(underlying, count=120)
    profile = volatility_threshold_profile(bars)
    entry_z = min(float(campaign.get("entry_zscore", 2.0)), profile["entry_zscore"])
    minimum_move = min(
        float(campaign.get("minimum_underlying_move", .02)),
        profile["minimum_underlying_move"],
    )
    regime = volatility_regime(owner, bars, entry_z, minimum_move)
    if not regime["active"]:
        return campaign, f"等待下一次动态波动激活：{regime['reason']}"
    if regime.get("strong_signal_override") and regime.get("trigger_confirmed"):
        direction = "CALL" if float(regime["intraday_return"]) > 0 else "PUT"
        direction_reason = f"强波动按日内方向选择 {direction}"
    elif regime.get("active"):
        direction = "CALL" if float(regime["direction_score"]) >= 2 else "PUT"
        direction_reason = f"动态状态按方向分选择 {direction}"
    else:
        direction, direction_reason, _ = automatic_option_direction(
            owner, bars, "VOLATILITY", minimum_move
        )
    if direction is None:
        return campaign, direction_reason
    expirations = broker.option_expirations(underlying).copy()
    min_dte = max(1, int(float(campaign.get("maturity_min_dte", 5) or 5)))
    max_dte = max(min_dte, int(float(campaign.get("maturity_max_dte", 21) or 21)))
    today = ny_today
    earliest = today + timedelta(days=min_dte)
    latest = today + timedelta(days=max_dte)
    valid = expirations[
        # 当日到期合约在收盘后不再作为新入场表达，避免反复生成已过期代码。
        (pd.to_datetime(expirations["strike_time"]).dt.date >= earliest)
        & (pd.to_datetime(expirations["strike_time"]).dt.date <= latest)
    ].sort_values("strike_time").head(5)
    if valid.empty:
        return campaign, "未来21天没有可滚动的期权到期日"
    chain = pd.concat(
        [broker.option_chain(underlying, str(expiry)) for expiry in valid["strike_time"]],
        ignore_index=True,
    )
    quotes = broker.snapshots(chain["code"].tolist())
    spot = float(owner["last_price"])
    # 阈值单源（P0-5 §3）：既有 `500/12/10` 字面量本批由 `candidate_quality:{tenant}`
    # 接管，**默认值 = 现值（行为不变）**。租户**显式入参**（`campaign["tenant_id"]`），
    # **禁止从 broker 反解** —— 那是 fail-closed 红线。
    # 解析失败（非法/链式校验不过）会抛 `CandidateQualityError`，**不静默回落默认值**。
    quality = resolve_candidate_quality_for_campaign(store, campaign)
    candidates = screen_gamma_contracts(
        chain, quotes, spot, direction,
        quality.hard_oi_min, quality.hard_spread_pct, quality.hard_moneyness_max,
    )
    option_history = pd.DataFrame(store.option_history_for_underlying(underlying))
    rv = realized_volatility(bars)
    # 硬排除 + 软降权（P0-5）：**只作用于「候选列表」，绝不「跳过 symbol」** ——
    # 已持仓的退出管理不受影响（A0 不变量，META 那次就是这么亏的）。
    # 放在评分**之前**：被排除的合约不参与排序、不进结构、不写 campaign，源头干净。
    # 复用 `:145` 已取回的批量 `quotes` 判报价停滞，**不新增任何行情调用**。
    candidates, exclusions = filter_candidates(
        candidates, quality, tenant_id=str(campaign.get("tenant_id") or ""),
        underlying=underlying,
    )
    if exclusions:
        try:
            store.record_candidate_exclusions(exclusions)
        except Exception as exc:  # noqa: BLE001
            # **选标的绝不能被留痕失败阻断**，但**绝不能静默** ——
            # 留痕写失败的直接后果是「候选质量页显示 0 条排除、而过滤照旧在排除」，
            # **那正是本需求要消灭的沉默过滤器**。级别用 ERROR：`operational_event`
            # 只落库、不触发飞书推送，不会刷屏。
            try:
                store.operational_event(
                    "volatility_roll", "CANDIDATE_EXCLUSION_RECORD_FAILED", "ERROR",
                    {
                        "tenant_id": str(campaign.get("tenant_id") or ""),
                        "underlying": underlying,
                        "excluded_count": len(exclusions),
                        "error": str(exc),
                    },
                )
            except Exception:  # noqa: BLE001 - 连事件都写不了时只能放弃，但不能影响选标的
                pass
    if candidates.empty:
        # 过滤到空 = **原样返回原 campaign** → 退出管理照常（设计 §2.5 / A0）。
        return _with_diagnostics(
            campaign, candidates, regime, "候选质量过滤后没有可评分的新合约"
        )
    candidates = rank_long_option_candidates(
        candidates, spot, profit_target=float(campaign.get("profit_target", .5)),
        stop_loss=float(campaign.get("stop_loss", .4)),
        realized_volatility=rv, regime=regime,
        holding_hours=float(campaign.get("expectancy_horizon_hours", 4)),
        option_history=option_history,
        # 显式传 NY 日期：DTE 直接喂给到期盈利概率 → 决定 probability / 排序。
        # 不依赖函数内兜底默认值，避免"调用方悄悄退回本机日期"。
        as_of=ny_today,
    )
    candidates = add_iv_history_percentiles(candidates, option_history)
    premium_budget, entry_tier = tiered_premium_budget(
        float(campaign.get("max_premium_per_stage", 1000)), regime, store
    )
    candidates = rank_volatility_contracts(
        candidates, spot, rv,
        ny_today,
        signal_active=bool(regime.get("active")),
        strong_signal=bool(
            regime.get("strong_signal_override") and regime.get("trigger_confirmed")
        ),
        max_premium_per_stage=premium_budget,
        contracts_per_stage=int(campaign.get("contracts_per_stage", 1)),
        store=store,
    )
    # 软降权必须进**主排序键**（P0-5 §6.0.1）：`screen_gamma_contracts` 里价差只是
    # tiebreaker，「排末尾」这个语义在候选层无法判定 —— 不前置就形同虚设。
    # **软降权 ≠ 硬排除**：被降权的合约**仍在候选池**，只是排在未降权之后。
    if "soft_demoted" in candidates.columns and not candidates.empty:
        sort_keys = ["soft_demoted"]
        ascending = [True]
        if "volatility_quality_score" in candidates.columns:
            sort_keys.append("volatility_quality_score")
            ascending.append(False)
        candidates = candidates.sort_values(
            sort_keys, ascending=ascending, kind="stable",
        ).reset_index(drop=True)
    if candidates.empty or "volatility_eligible" not in candidates:
        return _with_diagnostics(
            campaign, candidates, regime, "基础流动性筛选后没有可评分的新合约"
        )
    minimum_expected_return = volatility_min_expected_return(
        signal_active=bool(regime.get("active")),
        strong_signal=bool(
            regime.get("strong_signal_override") and regime.get("trigger_confirmed")
        ),
        store=store,
    )
    structures = build_volatility_structure_candidates(
        candidates, direction,
        minimum_expected_return=minimum_expected_return,
        premium_budget=premium_budget,
    )
    eligible = structures[structures["volatility_eligible"]] if not structures.empty else structures
    if eligible.empty:
        top_reason = str(candidates.iloc[0].get("volatility_filter_reason", ""))
        message = "没有合约同时通过成本、流动性、Delta、预期收益和权利金预算"
        if top_reason:
            message += f"；最高分候选：{top_reason}"
        return _with_diagnostics(campaign, candidates, regime, message)
    # ML 评分重排序（可选增强：AUC 0.76 的标的选择模型）
    eligible = _apply_ml_ranking(eligible, store)
    selected = eligible.iloc[0]
    selected_code = str(selected.get("code", ""))
    if parse_option_code(selected_code) is None:
        selected_legs = list(selected.get("legs") or [])
        selected_code = next(
            (
                str(leg.get("code", ""))
                for leg in selected_legs
                if str(leg.get("side", "")).upper() == "BUY"
                and parse_option_code(str(leg.get("code", ""))) is not None
            ),
            next(
                (
                    str(leg.get("code", ""))
                    for leg in selected_legs
                    if parse_option_code(str(leg.get("code", ""))) is not None
                ),
                "",
            ),
        )
    if parse_option_code(selected_code) is None:
        return _with_diagnostics(
            campaign, structures, regime, "最佳结构缺少可解析的主期权代码，跳过本轮维护"
        )
    selected_contract = pd.Series(selected.get("primary_row") or selected.to_dict())
    analysis = contract_analysis(
        selected_contract, spot, rv, store, peer_contracts=candidates
    )
    updated = dict(campaign)
    updated.update({
        "code": selected_code, "option_type": str(selected["option_type"]),
        "structure": str(selected.get("structure", "LONG_OPTION")),
        "structure_label": str(selected.get("structure_label", "单腿买方期权")),
        "legs": list(selected.get("legs") or []),
        "structure_net_debit": float(selected.get(
            "structure_net_debit", selected_contract.get("ask_price", 0)
        )),
        "structure_max_loss": float(selected.get(
            "structure_max_loss", market_number(selected_contract.get("ask_price")) * 100
        )),
        "event_date": ny_today.isoformat(), "strike_time": str(selected["strike_time"]),
        "entry_iv": float(analysis["隐含波动率"]),
        "entry_gamma_theta_efficiency": float(selected_contract.get("gamma_theta_efficiency", 0)),
        "regime_profile": regime, "optimizer_version": 2, "enabled": True,
        "adaptive_risk_tier": str(selected.get("adaptive_risk_tier", "常规")),
        "entry_iv_rv": float(selected.get("iv_rv", 0)),
        "entry_iv_rv_limit": float(selected.get("iv_rv_limit", 1.5)),
        "volatility_quality_score": float(selected.get("volatility_quality_score", 0)),
        "minimum_entry_quality_score": float(selected.get("volatility_min_quality_score", 54)),
        "model_expected_return": float(selected.get("model_expected_return", 0)),
        "estimated_profit_probability": float(selected.get("estimated_profit_probability", 0)),
        "expectancy_model_version": str(selected_contract.get("expectancy_model_version", "")),
        "expectancy_horizon_hours": float(campaign.get("expectancy_horizon_hours", 4)),
        "expectancy_empirical_samples": int(selected_contract.get("expectancy_empirical_samples", 0)),
        "iv_history_percentile": float(selected_contract.get("iv_history_percentile", 1)),
        "iv_percentile_status": str(selected_contract.get("iv_percentile_status", "CHAIN_PROXY")),
        "iv_history_samples": int(selected_contract.get("iv_history_samples", 0)),
        "maximum_entry_spread_pct": 6.0,
        "minimum_entry_open_interest": 800,
        "minimum_entry_volume": 10,
        "minimum_model_expected_return": float(selected.get(
            "minimum_model_expected_return",
            volatility_min_expected_return(
                signal_active=bool(regime.get("active")),
                strong_signal=bool(
                    regime.get("strong_signal_override") and regime.get("trigger_confirmed")
                ),
                store=store,
            ),
        )),
        "expected_return_policy": "TIERED",
        "budget_alternative": bool(selected_contract.get("budget_alternative", False)),
        "volatility_filter_reason": str(selected.get("volatility_filter_reason", "")),
        "last_expression_review": ny_today.isoformat(),
        "entry_tier": entry_tier,
        "base_premium_budget": float(campaign.get("max_premium_per_stage", 1000)),
        "effective_premium_budget": premium_budget,
    })
    # ML 评分写入活动（供审计与后续参考）
    if pd.notna(selected.get("ml_score")):
        updated["ml_score"] = float(selected["ml_score"])
        updated["ml_rank"] = str(selected.get("ml_rank", "NA"))
        if pd.notna(selected.get("ml_adjusted_score")):
            updated["ml_adjusted_score"] = float(selected["ml_adjusted_score"])
    if float(selected.get("iv_rv_limit", 1.5)) > 1.5:
        # Relaxed entries are deliberately capped at one contract and one stage.
        updated["contracts_per_stage"] = 1
        updated["max_stages"] = 1
        updated["adaptive_iv_relaxation"] = True
    for column in (
        "strike_price", "bid_price", "ask_price", "mid_price", "spread_pct",
        "moneyness_pct", "option_open_interest", "volume", "option_implied_volatility",
        "option_delta", "option_gamma", "option_theta", "option_vega", "option_rho",
        "option_contract_multiplier",
    ):
        if column in selected_contract:
            updated[column] = selected_contract[column]
    updated["_roll_diagnostics"] = _roll_diagnostics(
        structures, regime,
        f"已自动滚动到 {updated['code']}（{updated['structure_label']}）；{direction_reason}"
    )
    return updated, (
        f"已自动滚动到 {updated['code']}（{updated['structure_label']}）；{direction_reason}"
    )


def _apply_ml_ranking(structures: pd.DataFrame, store) -> pd.DataFrame:
    """
    用ML模型对合格结构做评分加权排序（可选增强）。
    
    - ML 未启用/模型缺失/评分失败时：原样返回，绝不影响主流程。
    - 启用时：为每个结构的主买入腿计算 ML 盈利概率，
      并生成 ml_adjusted_score = 质量分 + ML_WEIGHT*(ml_score-0.5)*10，
      即在质量分相近时（±约 ML_WEIGHT*5 分）由 ML 分数决定优先序。
    - 返回的 DataFrame 增加 ml_score / ml_rank / ml_adjusted_score 三列，
      排序按 ml_adjusted_score 降序，供审计诊断。
    """
    if structures is None or structures.empty:
        return structures
    try:
        from .ml_symbol_scorer import MLSymbolScorer
        scorer = MLSymbolScorer()
        if not scorer.enabled:
            return structures
    except Exception as e:  # noqa: BLE001
        # 模型加载失败不能阻断策略主流程
        try:
            import logging
            logging.getLogger(__name__).warning(
                "ML评分器不可用，跳过ML重排序: %s", e
            )
        except Exception:
            pass
        return structures

    # ML_WEIGHT 控制调整幅度（0=不调整，与原始排序一致）
    try:
        import os
        ml_weight = float(os.getenv("ML_WEIGHT", "0.6"))
    except Exception:  # noqa: BLE001
        ml_weight = 0.6

    def _main_leg_code(row) -> str:
        legs = list(row.get("legs") or [])
        for leg in legs:
            if str(leg.get("side", "")).upper() == "BUY":
                code = str(leg.get("code", ""))
                if parse_option_code(code) is not None:
                    return code
        for leg in legs:
            code = str(leg.get("code", ""))
            if parse_option_code(code) is not None:
                return code
        return str(row.get("code", ""))

    result = structures.copy()
    scores = []
    for _, row in result.iterrows():
        code = _main_leg_code(row)
        if not code:
            scores.append(None)
            continue
        try:
            s = scorer.score_symbol(code, None, store)
            scores.append(s)
        except Exception:  # noqa: BLE001
            scores.append(None)
    result["ml_score"] = scores
    result["ml_rank"] = [
        ("HIGH" if s is not None and s >= 0.7
         else "MEDIUM" if s is not None and s >= 0.5
         else "LOW" if s is not None else "NA")
        for s in scores
    ]
    # 加权调整分：质量分主导，ML 分数在 ±ml_weight*5 分容差内调整优先序
    def _adjusted(row):
        q = float(row.get("volatility_quality_score", 0) or 0)
        m = row.get("ml_score")
        if m is None:
            return q
        return q + float(ml_weight) * (float(m) - 0.5) * 10.0

    result["ml_adjusted_score"] = [_adjusted(row) for _, row in result.iterrows()]
    # 排序：调整分降序
    result = result.sort_values(
        "ml_adjusted_score", ascending=False, na_position="last"
    ).reset_index(drop=True)
    return result
