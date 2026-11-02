from __future__ import annotations

import logging
import os

import pandas as pd

logger = logging.getLogger(__name__)

from .option_lifecycle import parse_option_code


DEFAULT_BUDGETS = [
    {"strategy_id": "core_satellite", "capital_ratio": 0.65, "max_loss_ratio": 0.15,
     "option_premium_ratio": 0.00, "enabled": True},
    {"strategy_id": "earnings_gamma", "capital_ratio": 0.10, "max_loss_ratio": 0.03,
     "option_premium_ratio": 0.10, "enabled": True},
    {"strategy_id": "volatility_long_gamma", "capital_ratio": 0.10, "max_loss_ratio": 0.03,
     "option_premium_ratio": 0.10, "enabled": True},
    {"strategy_id": "earnings_gamma_manual", "capital_ratio": 0.05, "max_loss_ratio": 0.02,
     "option_premium_ratio": 0.05, "enabled": True},
    {"strategy_id": "SMA Cross", "capital_ratio": 0.02, "max_loss_ratio": 0.01,
     "option_premium_ratio": 0.00, "enabled": True},
    {"strategy_id": "RSI Mean Reversion", "capital_ratio": 0.015, "max_loss_ratio": 0.01,
     "option_premium_ratio": 0.00, "enabled": True},
    {"strategy_id": "20 Day Breakout", "capital_ratio": 0.015, "max_loss_ratio": 0.01,
     "option_premium_ratio": 0.00, "enabled": True},
]


def ensure_default_budgets(store) -> list[dict]:
    current = store.strategy_risk_budgets()
    if not current:
        store.save_strategy_risk_budgets(DEFAULT_BUDGETS)
        store.set_setting("strategy_budget_schema_version", "2")
        current = store.strategy_risk_budgets()
    elif int(store.get_setting("strategy_budget_schema_version", "1") or 1) < 2:
        migrated = []
        for item in current:
            row = dict(item)
            if row["strategy_id"] == "core_satellite" and abs(float(row["capital_ratio"]) - 0.55) < 1e-9:
                row["capital_ratio"] = 0.65
            migrated.append(row)
        store.save_strategy_risk_budgets(migrated)
        store.set_setting("strategy_budget_schema_version", "2")
        current = store.strategy_risk_budgets()
    return current


class AccountCurrencyMismatch(RuntimeError):
    """账户资产快照不是 USD 口径 —— **fail-closed**，拒绝据此计算/放行任何美元限额。"""


#: 币种判定三态：显式非 USD / 无法判定 / 可用。
CURRENCY_OK = "OK"
CURRENCY_MISMATCH = "MISMATCH"
CURRENCY_UNKNOWN = "UNKNOWN"


def account_currency_status(account) -> tuple[str, str]:
    """判断账户快照的币种能否安全当作 USD 使用，返回 `(状态, 实际币种)`。

    2026-11-02 实盘复盘：`accinfo_query` 不传 currency 时返回**账户本位币**（该账户为 HKD），
    而 `positions()` 的 market_val 是美元 —— 混算让「账户资产」放大 7.85 倍，
    「压力损失预算」这类按权益百分比算的闸门被静默放宽 7.85 倍（10-29/10-30 实盘
    就是靠这个虚高额度在超配开仓）。`broker.py` 已显式传 `currency="USD"`，
    但**返回值是否真被采纳没人校验** —— 本函数补这道校验。

    **三态而不是二态**（Reviewer 2026-11-02）：字段缺失 / 快照为空 / 结构异常
    不能当作「没问题」，那正是 7.85 倍 bug 的复发路径；但也不能一律拒（会把模拟盘跑挂）。
    因此把「无法判定」单独拎出来，由调用方按**账户环境**分流：

    - `CURRENCY_OK`：`USD` / `N/A`；
    - `CURRENCY_MISMATCH`：显式非 USD（如 HKD）—— 任何环境都拒；
    - `CURRENCY_UNKNOWN`：空快照、无 `currency` 列、取字段异常 —— 实盘拒 + 强告警，模拟盘放行 + WARN。
    """
    try:
        if account is None or getattr(account, "empty", True):
            return CURRENCY_UNKNOWN, ""
        if "currency" not in account.columns:
            return CURRENCY_UNKNOWN, ""
        actual = str(account.iloc[0].get("currency") or "").strip().upper()
    except Exception:  # noqa: BLE001 - 结构异常 = 无法判定，交调用方按环境分流
        return CURRENCY_UNKNOWN, ""
    if actual in ("USD", "N/A"):
        return CURRENCY_OK, actual
    if not actual:
        return CURRENCY_UNKNOWN, actual
    return CURRENCY_MISMATCH, actual


def _is_real_account(broker) -> bool:
    return str(getattr(broker, "trading_env", "") or "").upper().endswith("REAL")


def _report_currency_problem(store, broker, status: str, actual: str) -> None:
    """币种异常留痕 + 强告警（带 incident 去重，不每轮刷屏）。"""
    account = getattr(broker, "account_id", "?")
    detail = (
        f"账户 {account} 资产快照币种无法确认为 USD（状态={status}，实际={actual or '空'}）："
        "美元限额与百分比仓位可能被汇率倍数放大，已按 fail-closed 拒绝开仓。"
    )
    try:
        store.operational_event(
            "portfolio_allocator", "ACCOUNT_CURRENCY_UNVERIFIED", "ERROR",
            {"account_id": account, "status": status, "currency": actual},
        )
    except Exception:  # noqa: BLE001 - 留痕失败不得阻断门禁判定
        pass
    try:
        from .alerts import AlertManager

        AlertManager(store).send(
            "ERROR", "ACCOUNT_CURRENCY_UNVERIFIED", detail,
            incident_key=f"ACCOUNT_CURRENCY_UNVERIFIED:{account}",
        )
    except Exception:  # noqa: BLE001 - 告警失败不得把交易链路打断
        pass


def strategy_budget_usage(store, broker) -> tuple[pd.DataFrame, float]:
    budgets = ensure_default_budgets(store)
    try:
        account = broker.account_info()
        positions = broker.positions()
    except Exception:
        cached_account = store.broker_snapshot("account_info") or {}
        cached_positions = store.broker_snapshot("positions") or {}
        account = pd.DataFrame(cached_account.get("payload", []))
        positions = pd.DataFrame(cached_positions.get("payload", []))
    status, actual_currency = account_currency_status(account)
    if status == CURRENCY_MISMATCH or (status == CURRENCY_UNKNOWN and _is_real_account(broker)):
        raise AccountCurrencyMismatch(
            f"账户资产快照币种无法安全当作 USD（状态={status}，实际={actual_currency or '空'}）："
            "美元限额与百分比仓位（含压力损失预算）会按汇率倍数失真，已拒绝开仓。"
            f"请确认 accinfo_query 的 currency=USD 是否被采纳（账户={getattr(broker, 'account_id', '?')}）。"
        )
    if status == CURRENCY_UNKNOWN:
        logger.warning(
            "账户 %s 资产快照币种无法判定（模拟盘放行）：美元限额可能失真，请核对 accinfo_query 返回。",
            getattr(broker, "account_id", "?"),
        )
    equity = float(account.iloc[0].get("total_assets", 0) or 0) if not account.empty else 0.0
    symbols = positions["code"].astype(str).tolist() if not positions.empty else []
    quotes = broker.snapshots(symbols) if symbols else pd.DataFrame()
    quote_map = {
        str(row["code"]): float(row.get("last_price", 0) or 0)
        for _, row in quotes.iterrows()
    } if not quotes.empty else {}
    position_map = {
        str(row["code"]): row for _, row in positions.iterrows()
    } if not positions.empty else {}
    usage = {item["strategy_id"]: {"capital_used": 0.0, "loss_used": 0.0,
                                           "option_premium_used": 0.0} for item in budgets}
    # 租户隔离铁律：预算账本必须与当前 broker 同环境同账户，严禁把模拟沙盒历史账本计入实盘配额（反之亦然）
    broker_env = str(getattr(broker, "trading_env", "") or "").upper()
    broker_account = getattr(broker, "account_id", None)
    ledger_positions = store.virtual_positions(
        env=broker_env or None,
        account_id=broker_account,
    )
    for item in ledger_positions:
        strategy, symbol = item["strategy_id"], item["symbol"]
        if strategy not in usage:
            continue
        position = position_map.get(symbol)
        fallback = float(position.get("nominal_price", position.get("cost_price", 0)) or 0) \
            if position is not None else float(item.get("average_cost", 0) or 0)
        price = quote_map.get(symbol, fallback)
        quantity = abs(float(item["quantity"]))
        is_option = parse_option_code(symbol) is not None
        value = quantity * price * (100 if is_option else 1)
        usage[strategy]["capital_used"] += value
        usage[strategy]["loss_used"] += value if is_option else value * float(
            os.getenv("STOCK_STRESS_LOSS_RATIO", "0.10")
        )
        if is_option:
            usage[strategy]["option_premium_used"] += value
    rows = []
    for budget in budgets:
        values = usage[budget["strategy_id"]]
        rows.append({
            **budget, **values,
            "capital_limit": equity * float(budget["capital_ratio"]),
            "loss_limit": equity * float(budget["max_loss_ratio"]),
            "option_premium_limit": equity * float(budget["option_premium_ratio"]),
            "capital_utilization": values["capital_used"] / (equity * float(budget["capital_ratio"]))
            if equity > 0 and budget["capital_ratio"] else 0,
            "loss_utilization": values["loss_used"] / (equity * float(budget["max_loss_ratio"]))
            if equity > 0 and budget["max_loss_ratio"] else 0,
        })
    return pd.DataFrame(rows), equity


def _account_for_currency_check(store, broker):
    """取账户快照用于币种校验：live 优先，失败回落缓存 —— 两条都必须校验。"""
    try:
        return broker.account_info()
    except Exception:  # noqa: BLE001 - 断线回落快照，但仍要过币种门禁
        cached = store.broker_snapshot("account_info") or {}
        return pd.DataFrame(cached.get("payload", []))


def strategy_budget_allowed(
    store, broker, strategy_id: str | None, symbol: str, side: str,
    quantity: int, price: float, multiplier: float,
) -> tuple[bool, str, dict]:
    if side == "SELL" or not strategy_id:
        return True, "减仓不占用新增策略预算", {}
    # 币种门禁**必须排在预算早退之前**：否则「未配置三维预算」的账户会绕过它，
    # 而这条守卫的前提正是「任何美元限额都不能建立在非 USD 权益上」（2026-11-02 复盘）。
    #
    # 适用范围：**只对能报账户快照的 broker 走正常校验**。生产 `FutuPaperBroker`
    # 一定有 `account_info()`；测试替身/轻量 broker 没有该方法时按「本层不适用」跳过，
    # 否则任何没有账户信息的替身都会被判成"实盘币种未知"，把无关用例全打红。
    # 断线（有方法但抛错）不算"不适用"：会回落缓存快照，仍然过同一道门禁。
    #
    # **但实盘例外**（Reviewer 2026-11-02）：实盘 broker 若根本没有 `account_info()`，
    # 等于币种无从求证 —— 必须按 UNKNOWN 直接拒。否则将来谁包一层代理 broker，
    # 这条门禁就静默失效，7.85 倍的老毛病原样复发。
    if not hasattr(broker, "account_info"):
        if _is_real_account(broker):
            _report_currency_problem(store, broker, CURRENCY_UNKNOWN, "")
            return False, (
                "实盘 broker 不提供账户快照（无 account_info）：无法确认币种是否为 USD，"
                "不能排除美元限额被汇率倍数放大，已拒绝开仓。"
            ), {"currency_mismatch": True, "currency": "", "currency_unknown": True}
    else:
        status, actual_currency = account_currency_status(
            _account_for_currency_check(store, broker)
        )
        if status == CURRENCY_MISMATCH:
            _report_currency_problem(store, broker, status, actual_currency)
            return False, (
                f"账户资产快照币种为 {actual_currency}，不是 USD：所有美元限额与百分比仓位"
                "（含压力损失预算）都会按汇率倍数失真，已拒绝开仓。"
            ), {"currency_mismatch": True, "currency": actual_currency}
        if status == CURRENCY_UNKNOWN:
            if _is_real_account(broker):
                # 实盘取不到币种 = 无法排除 7.85 倍复发 → 拒开仓 + 强告警（incident 去重）。
                _report_currency_problem(store, broker, status, actual_currency)
                return False, (
                    "实盘账户资产快照无法确认币种是否为 USD：不能排除美元限额被汇率倍数放大，"
                    "已拒绝开仓。请核对 accinfo_query 的 currency=USD 是否被采纳。"
                ), {"currency_mismatch": True, "currency": "", "currency_unknown": True}
            logger.warning(
                "模拟盘账户 %s 资产快照币种无法判定：放行，但美元限额可能失真，请核对 accinfo_query。",
                getattr(broker, "account_id", "?"),
            )
    budget = store.strategy_risk_budget(strategy_id)
    if budget is None:
        return True, "未配置三维策略预算，沿用原有资金配额", {}
    if not budget["enabled"]:
        return False, f"{strategy_id} 已在组合资金分配器中停用", {}
    usage, equity = None, 0.0
    try:
        usage, equity = strategy_budget_usage(store, broker)
    except AccountCurrencyMismatch as exc:
        # fail-closed：币种不是 USD 时绝不能按虚高权益放行，但也不能把异常抛进主循环。
        return False, str(exc), {"currency_mismatch": True}
    current = usage[usage["strategy_id"] == strategy_id]
    if current.empty or equity <= 0:
        return False, "无法计算策略预算使用率", {}
    row = current.iloc[0]
    notional = quantity * price * multiplier
    is_option = parse_option_code(symbol) is not None
    projected = {
        "capital": float(row["capital_used"]) + notional,
        "loss": float(row["loss_used"]) + (
            notional if is_option else notional * float(os.getenv("STOCK_STRESS_LOSS_RATIO", "0.10"))
        ),
        "option_premium": float(row["option_premium_used"]) + (notional if is_option else 0),
        "capital_limit": float(row["capital_limit"]),
        "loss_limit": float(row["loss_limit"]),
        "option_premium_limit": float(row["option_premium_limit"]),
    }
    if projected["capital"] > projected["capital_limit"]:
        return False, f"{strategy_id} 资金占用将超过组合配额", projected
    if projected["loss"] > projected["loss_limit"]:
        return False, f"{strategy_id} 压力损失预算将超过上限", projected
    if is_option and projected["option_premium"] > projected["option_premium_limit"]:
        return False, f"{strategy_id} 期权权利金预算将超过上限", projected
    return True, "策略资金、损失与期权权利金预算通过", projected
