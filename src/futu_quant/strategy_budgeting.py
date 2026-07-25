from __future__ import annotations


STRATEGY_GROUP_LABELS = {
    "core_satellite": "核心股票组合",
    "volatility_long_gamma": "主力期权策略",
    "earnings_gamma": "财报事件策略（暂非重心）",
    "earnings_gamma_manual": "财报手工单（暂非重心）",
    "SMA Cross": "示例股票策略",
    "RSI Mean Reversion": "示例股票策略",
    "20 Day Breakout": "示例股票策略",
}

RECOMMENDED_ACTIVE_BUDGETS = {
    "core_satellite": {
        "capital_ratio": 0.65,
        "max_loss_ratio": 0.15,
        "option_premium_ratio": 0.00,
        "enabled": True,
    },
    "volatility_long_gamma": {
        "capital_ratio": 0.25,
        "max_loss_ratio": 0.06,
        "option_premium_ratio": 0.25,
        "enabled": True,
    },
    "earnings_gamma": {
        "capital_ratio": 0.00,
        "max_loss_ratio": 0.03,
        "option_premium_ratio": 0.00,
        "enabled": False,
    },
    "earnings_gamma_manual": {
        "capital_ratio": 0.00,
        "max_loss_ratio": 0.02,
        "option_premium_ratio": 0.00,
        "enabled": False,
    },
    "SMA Cross": {
        "capital_ratio": 0.00,
        "max_loss_ratio": 0.01,
        "option_premium_ratio": 0.00,
        "enabled": False,
    },
    "RSI Mean Reversion": {
        "capital_ratio": 0.00,
        "max_loss_ratio": 0.01,
        "option_premium_ratio": 0.00,
        "enabled": False,
    },
    "20 Day Breakout": {
        "capital_ratio": 0.00,
        "max_loss_ratio": 0.01,
        "option_premium_ratio": 0.00,
        "enabled": False,
    },
}


def recommended_active_budget_rows(current_rows: list[dict]) -> list[dict]:
    """Return current rows with the recommended strategy allocation applied."""
    seen = {str(row["strategy_id"]) for row in current_rows}
    rows = []
    for row in current_rows:
        item = dict(row)
        preset = RECOMMENDED_ACTIVE_BUDGETS.get(str(item["strategy_id"]))
        if preset:
            item.update(preset)
        rows.append(item)
    for strategy_id, preset in RECOMMENDED_ACTIVE_BUDGETS.items():
        if strategy_id not in seen:
            rows.append({"strategy_id": strategy_id, **preset})
    return rows


def budget_group_label(strategy_id: str) -> str:
    return STRATEGY_GROUP_LABELS.get(str(strategy_id), "其他策略")
