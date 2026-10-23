"""回归：core_satellite 的两个真金路径 bug（10-23 03:45 窗口实测暴露）。

背景（Chief/Ops 在 03:45 调仓窗口实测）：
窗口内 `core_satellite` 反复报错、**零委托** —— 不是配置问题，是代码 bug：

1. `UnboundLocalError: cannot access local variable 'used'`
   —— `check_real_trade` 的成功返回无条件引用 `used`，但该变量只在
   **BUY 分支**定义；SELL 分支定义的是 `used_reduction`。
   根因是 P0-4（减仓/开仓额度分离）分流后**漏改成功返回**。
   影响：**任何实盘 SELL 走到门禁成功路径就崩** —— 含 core_satellite 的正股减仓。

2. `could not convert string to float: 'N/A'`
   —— `run_strategies.py` 直接 `float(info["total_assets"])`，
   模拟盘的该字段返回 `'N/A'` → 整轮策略失败、零委托。

本文件锁定这两条。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from futu_quant.real_trading import check_real_trade  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402


def _real_settings(monkeypatch, **overrides):
    from dataclasses import replace

    from futu_quant.config import Settings

    monkeypatch.setenv("FUTU_TRADING_ENV", "REAL")
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", "281756481476169109")
    monkeypatch.setenv("REAL_ALLOWED_STRATEGIES", "core_satellite,volatility_long_gamma")
    monkeypatch.setenv("REAL_MAX_ORDER_NOTIONAL", "2500")
    monkeypatch.setenv("REAL_MAX_DAILY_NOTIONAL", "5000")
    monkeypatch.setenv("REAL_REQUIRE_MANUAL_CONFIRM", "false")
    loaded = Settings.load(refresh=True)
    return replace(loaded, **overrides) if overrides else loaded


def test_sell_passes_gate_without_unbound_local(tmp_path, monkeypatch):
    """Bug 1：实盘 SELL 走到门禁成功路径不得抛 UnboundLocalError。

    这是 03:45 窗口 core_satellite 反复报错、零委托的直接原因。
    """
    from datetime import datetime, timezone

    from futu_quant.real_trading import confirmation_code, enable_session

    settings = _real_settings(monkeypatch, real_require_manual_confirm=True)
    store = TradingStore(tmp_path / "sell_gate.db")
    now = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)
    enable_session(store, settings, confirmation_code(settings, now), now=now)

    decision = check_real_trade(
        store, settings, "core_satellite", "US.NVDA", "SELL", 1, 15.0,
        {"account_id": "281756481476169109"}, now=now,
    )
    # 关键：不得抛异常（旧代码在此抛 UnboundLocalError）
    assert decision.allowed is True
    assert "used_today" in decision.details


def test_buy_gate_still_reports_entry_usage(tmp_path, monkeypatch):
    """回归：BUY 仍必须报告开仓额度占用（不得因改名丢字段）。"""
    from datetime import datetime, timezone

    from futu_quant.real_trading import confirmation_code, enable_session

    settings = _real_settings(monkeypatch, real_require_manual_confirm=True)
    store = TradingStore(tmp_path / "buy_gate.db")
    now = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)
    enable_session(store, settings, confirmation_code(settings, now), now=now)

    decision = check_real_trade(
        store, settings, "core_satellite", "US.NVDA", "BUY", 1, 15.0,
        {"account_id": "281756481476169109"}, now=now,
    )
    assert decision.allowed is True
    assert decision.details["used_today"] == 0.0


def test_na_total_assets_does_not_crash_and_is_loud(tmp_path):
    """Bug 2：券商 `total_assets='N/A'` 不得让策略轮次崩，且必须明确报错。

    旧代码 `float(info["total_assets"])` 抛
    "could not convert string to float: 'N/A'" → core_satellite_sim 每轮失败。
    新行为：解析失败 → 抛**可读的** RuntimeError（带原始值），
    由调度器记为 EXECUTION_SKIPPED，而不是一个裸 ValueError。
    """
    src = (ROOT / "scripts" / "run_strategies.py").read_text(encoding="utf-8")
    # 不得再对 total_assets 做裸 float()
    assert 'float(info.iloc[0]["total_assets"])' not in src, (
        "total_assets 仍在做裸 float() 转换 —— 券商会返回 'N/A'"
    )
    assert "market_number" in src, "未使用容错转换 market_number"


def test_broker_field_parsing_is_defensive():
    """券商字段容错：'N/A' / None / 空串都必须安全返回默认值。"""
    from futu_quant.risk import market_number

    assert market_number("N/A", 0.0) == 0.0
    assert market_number(None, 0.0) == 0.0
    assert market_number("", 0.0) == 0.0
    assert market_number("123.45", 0.0) == 123.45
    assert market_number(float("nan"), 0.0) == 0.0, "NaN 必须回落默认值"


def test_account_value_guard_rejects_zero_funding():
    """资金为 0 时必须拒绝执行调仓（不得带 0 资金跑）。"""
    src = (ROOT / "scripts" / "run_strategies.py").read_text(encoding="utf-8")
    assert "拒绝以 0 资金执行调仓" in src, "缺少 0 资金拒绝守卫"
