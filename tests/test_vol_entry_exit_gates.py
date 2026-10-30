"""2026-10-30 入场/退场三处收紧的回归。

背景（当日实盘 4 笔全部硬止损，合计 -194 毛 / -214 净）：

1. 软保护启动线 8% 与硬止损线 8% **重合** → 浮盈 0%~8% 是完全真空区。
   TSM261106P410000 峰值 +5.62% 落在真空里，一路回到 -13.6% 才离场。
2. `strong_override` 含 `|intraday| >= 3%` → 当日跌 3% 就获得强波动档的三重放宽
   （量比门槛 1.2→0.8、方向分不足 2 也放行、入场层级升档）。4 笔全部命中该分支，
   而 z 值只有 1.61~1.89（低于动态门槛 2.3）。
3. 止损线 = min($300, 8%×权利金) 可能**窄于一个盘口价差** → 一次报价抖动打掉整条止损线。
   TSM 止损线 $58.32、出口价差 $60.00。

本文件锁住的是「改动后的判定」，不是「当天那几笔的具体数字」：
数值来自当天真实盘口，用来保证用例不脱离现实。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from futu_quant.volatility_optimizer import (
    ENTRY_MAX_SPREAD_PCT,
    ENTRY_MIN_SPREAD_MULTIPLE,
    decide_volatility_action,
    volatility_entry_spread_gate,
    volatility_regime,
)


# --------------------------------------------------------------------------
# ③ 入场价差门
# --------------------------------------------------------------------------

def test_entry_spread_gate_blocks_when_stop_cannot_cover_two_and_half_spreads():
    """COIN 当日盘口原值：买一 8.90 / 卖一 9.35，1 张。

    止损额 = min(300, 8% × 935) = 74.80；价差 = $45.00；2.5× = $112.50 > 74.80。
    → 止损线只够 1.66 个价差，一次报价抖动即可打掉，必须拦。
    """
    allowed, reason, detail = volatility_entry_spread_gate(
        bid=8.90, ask=9.35, quantity=1, max_loss_usd=300.0,
    )
    assert allowed is False
    assert "撑不起" in reason
    assert round(detail["spread_usd"], 2) == 45.00
    assert round(detail["stop_usd"], 2) == 74.80
    assert detail["spread_pct"] < ENTRY_MAX_SPREAD_PCT  # 价差本身没超上限，是被止损额挡的


def test_entry_spread_gate_rejects_spread_above_cap():
    """价差占中间价 5.83% > 上限 5%，即使止损额够宽也要拦。"""
    allowed, reason, detail = volatility_entry_spread_gate(
        bid=1.00, ask=1.06, quantity=10, max_loss_usd=300.0,
    )
    assert allowed is False
    assert "超过上限" in reason
    assert detail["spread_pct"] > ENTRY_MAX_SPREAD_PCT


def test_entry_spread_gate_passes_tsm_like_book():
    """TSM 入场时盘口：买一 7.20 / 卖一 7.40（价差 2.7%）。

    止损额 = min(300, 8% × 740) = 59.20 ≥ 2.5 × $20.00 = $50.00 → 放行。
    （它当天亏在**出口时**价差扩张到 9.1%，那是止损实现问题，不是入场门禁问题。）
    """
    allowed, reason, detail = volatility_entry_spread_gate(
        bid=7.20, ask=7.40, quantity=1, max_loss_usd=300.0,
    )
    assert allowed is True, reason
    assert round(detail["stop_usd"], 2) == 59.20
    assert round(detail["spread_usd"], 2) == 20.00


def test_entry_spread_gate_passes_nvda_like_book_at_the_edge():
    """NVDA 入场盘口：买一 3.10 / 卖一 3.20。

    止损额 25.60 vs 2.5 × $10.00 = $25.00 —— 只差 $0.60 险过。
    **故意保留这条边界**：它说明「2.5 倍」这个门槛对 ~$300 权利金的仓位非常紧，
    再收紧 0.1 倍就会把这类仓位全部挡掉。
    """
    allowed, _reason, detail = volatility_entry_spread_gate(
        bid=3.10, ask=3.20, quantity=1, max_loss_usd=300.0,
    )
    assert allowed is True
    margin = detail["stop_usd"] - ENTRY_MIN_SPREAD_MULTIPLE * detail["spread_usd"]
    assert round(margin, 2) == 0.60


def test_entry_spread_gate_skips_unusable_book():
    """盘口不可用时不重复拦：可用性由 option_book_status 负责，避免同一原因两条记录。"""
    allowed, _reason, detail = volatility_entry_spread_gate(
        bid=0.0, ask=0.0, quantity=1, max_loss_usd=300.0,
    )
    assert allowed is True
    assert detail["skipped"] is True


def test_entry_spread_gate_scales_with_quantity():
    """张数放大后价差金额同步放大，止损额被 $300 封顶 → 大仓位更早被拦。"""
    allowed, reason, detail = volatility_entry_spread_gate(
        bid=8.90, ask=9.35, quantity=3, max_loss_usd=300.0,
    )
    assert allowed is False
    assert round(detail["stop_usd"], 2) == 224.40  # 未触及 $300 封顶，但仍 < 2.5 × $135
    assert "撑不起" in reason


# --------------------------------------------------------------------------
# ② 强信号覆盖不再由「当日涨跌 3%」触发
# --------------------------------------------------------------------------

def _bars(closes: list[float]) -> pd.DataFrame:
    series = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "close": series,
        "high": series + 0.5,
        "low": series - 0.5,
        "volume": [1_000_000] * len(closes),
    })


def test_daily_three_percent_move_no_longer_counts_as_strong_signal():
    """日内 -4%、但 z 与 ATR 都没到强信号线 → 不得再拿「强波动」档的三重放宽。

    构造：收盘在 100 / 96 之间来回（20 日收益标准差 ≈ 4.3%），
    最后一根 96 对前收 100 即 -4%；此时 z ≈ 0.94、ATR 倍数 ≈ 0.9，都远低于
    z≥3 / ATR≥2 的强信号线。改动前 `abs(intraday) >= .03` 会让它变成强信号。
    """
    bars = _bars([100.0, 96.0] * 45)
    owner = {"last_price": 96.0, "prev_close_price": 100.0}
    regime = volatility_regime(owner, bars, entry_z=2.3, minimum_move=0.04)

    assert abs(regime["intraday_return"]) >= 0.03  # 前提：当日确实跌超 3%
    assert regime["zscore_20d"] < 3.0
    assert regime["atr_ratio"] < 2.0
    assert regime["strong_signal_override"] is False


def test_strong_signal_still_available_from_zscore_or_atr():
    """收紧只删掉「跌幅」这一支：z≥3 或 ATR≥2 仍可获得强信号覆盖。"""
    bars = _bars([90.0] * 90)
    owner = {"last_price": 96.0, "prev_close_price": 100.0}
    regime = volatility_regime(owner, bars, entry_z=2.0, minimum_move=.02)
    # 恒定收盘使当日涨跌远超 20 日 ATR → atr_ratio 很大
    assert regime["atr_ratio"] >= 2.0
    assert regime["strong_signal_override"] is True


# --------------------------------------------------------------------------
# ① 软启动锁盈：8% → 4%
# --------------------------------------------------------------------------

def _decide(soft_start: float, *, peak: float, pnl: float):
    now = datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc)
    return decide_volatility_action(
        now=now,
        opened_at=now - timedelta(minutes=5),
        quantity=1,
        stages_filled=1,
        max_stages=1,
        pnl_ratio=pnl,
        peak_pnl_ratio=peak,
        drawdown_from_peak=max(0.0, (peak - pnl) / peak) if peak > 0 else 0.0,
        directional_confirmation=True,
        signal_active=True,
        profit_target=2.0,
        stop_loss=0.35,
        trailing_stop=0.25,
        max_hold_hours=24,
        profit_protection_start=0.10,
        profit_protection_drawdown=0.18,
        minimum_locked_profit=0.06,
        single_contract_take_profit=0.18,
        dynamic_stop_enabled=False,
        soft_profit_protection_start=soft_start,
        soft_profit_lock_floor=0.03,
        soft_profit_giveback=0.35,
    )


def test_soft_protection_now_covers_tsm_peak_of_five_point_six_percent():
    """TSM 峰值 +5.62% 回落到 +3% 时：新口径必须锁盈离场。"""
    action, reason = _decide(0.04, peak=0.0562, pnl=0.03)
    assert action == "SELL"
    assert "软启动锁盈" in reason


def test_old_eight_percent_threshold_would_hold_that_same_position():
    """同一笔在旧口径（8%）下什么都不做 —— 这正是「赢了没有止盈」的证据。"""
    action, _reason = _decide(0.08, peak=0.0562, pnl=0.03)
    assert action == "HOLD"


def test_soft_protection_still_ignores_tiny_peaks():
    """峰值 3% 仍不启动软保护：门槛下降不等于取消门槛。"""
    action, _reason = _decide(0.04, peak=0.03, pnl=0.01)
    assert action == "HOLD"


# --------------------------------------------------------------------------
# 配置侧守卫：三个新默认值必须写进 radar seed，避免「配置看不到、行为却变了」
# --------------------------------------------------------------------------

def test_scanner_seed_carries_soft_protection_and_spread_gate_keys():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "futu_quant" / "volatility_scanner.py"
    text = source.read_text(encoding="utf-8")
    for key in (
        "volatility_default_soft_profit_protection_start",
        "volatility_default_soft_profit_lock_floor",
        "volatility_default_soft_profit_giveback",
        "volatility_default_entry_max_spread_pct",
        "volatility_default_entry_min_spread_multiple",
    ):
        assert key in text, f"radar seed 未写入 {key}：新 campaign 会退回代码默认值"


def test_gamma_strategy_uses_four_percent_soft_start_fallback():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "scripts" / "run_gamma_strategy.py"
    text = source.read_text(encoding="utf-8")
    assert 'campaign.get("soft_profit_protection_start", .04)' in text, (
        "软保护启动线回落值必须是 4%：8% 与硬止损线重合，会让浮盈 0~8% 全程裸奔"
    )
