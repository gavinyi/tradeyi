"""硬止损取价口径：**mid 判真跌、bid 判价差假摔**（2026-10-30 实盘复盘）。

复盘对象（真实成交）：
    US.COIN261120P140000，入场 1 张 @ 9.15（权利金 $915）
    止损线 = min($300, 8%×$915) = **$73.20**（对应 8.42）
    13:50:48  bid 8.80（-3.8%）
    13:51:56  bid 8.40（-8.2%）→ 触发止损
    13:52:02  成交 8.79          → 实际只亏 $36（毛）/ $41（净）
    当时中间价约 8.60（-6.0%），**根本没击穿止损线**

也就是说：判定用 bid（8.40）亏 $75，实际成交（8.79）亏 $41 —— 中间差的 $39
就是盘口价差，不是行情。COIN 期权价差常年 5–7%，而止损线只有 8%，
**止损线落在噪音带里**，任何一次报价抖动都能打掉仓位。

修复后的判定规则（本文件逐条锁死）：
1. mid 击穿 → 立即触发（真跌不能等）；
2. 仅 bid 击穿（mid 未击穿）→ 需连续 N 轮确认，单轮只记待确认；
3. bid 缺失/无有效 ask → mid 退化为可成交价，等价旧行为立即判定（保守）；
4. 止损线 < 2×价差成本 → spread_guard=True（供调用方告警）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

from futu_quant.volatility_optimizer import (
    HARD_STOP_BID_CONFIRM_CHECKS,
    volatility_hard_loss_check,
)
from futu_quant.store import TradingStore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

# 复盘用的真实数值
COST = 9.15
STOP_USD = 73.2          # min(300, 8% × 915)
STOP_RATIO = 0.25


def _check(bid: float, ask: float, *, last: float | None = None, pending: int = 0,
           usd: float = STOP_USD, ratio: float = STOP_RATIO,
           required: int = HARD_STOP_BID_CONFIRM_CHECKS) -> dict:
    return volatility_hard_loss_check(
        bid=bid, ask=ask, last=last if last is not None else bid,
        cost=COST, quantity=1, multiplier=100,
        max_loss_usd=usd, max_loss_ratio=ratio,
        pending_checks=pending, required_checks=required,
    )


def test_coin_case_first_cycle_only_flags_pending_not_fire():
    """复盘重放：bid 8.40 / ask 8.80（mid 8.60）→ 不得当场止损。"""
    result = _check(8.40, 8.80)

    assert result["mid"] == pytest.approx(8.60)
    assert result["pnl_ratio_mid"] == pytest.approx(-0.0601, abs=1e-3)
    assert result["pnl_amount_mid"] == pytest.approx(-55.0)
    # bid 口径确实击穿了（-8.2% / -$75）
    assert result["pnl_amount_bid"] == pytest.approx(-75.0)
    assert result["fire"] is False, "仅 bid 击穿时不得当场离场 —— 这正是 107 秒被扫出场的根因"
    assert result["basis"] == "BID_PENDING"
    assert result["pending_checks"] == 1
    assert result["reason"] is None


def test_coin_case_second_consecutive_cycle_fires():
    """连续第二轮仍只有 bid 在下方 → 确认后离场（不放弃风控）。"""
    result = _check(8.40, 8.80, pending=1)

    assert result["fire"] is True
    assert result["basis"] == "BID"
    assert "单笔硬止损触发" in result["reason"]
    assert "仅可成交价" in result["reason"]
    assert "中间价 8.60" in result["reason"]
    assert result["pending_checks"] == 0, "触发后必须清零，否则下一笔会带着确认进度"


def test_genuine_collapse_fires_immediately_on_mid():
    """真跌（mid 也击穿）必须**立即**触发，不允许等第二轮。"""
    result = _check(7.00, 7.20)  # mid 7.10 → -22.4%，绝对额 -$205

    assert result["fire"] is True
    assert result["basis"] == "MID", "mid 击穿属于真实价格位移，不能走确认流程拖延止损"
    assert result["pending_checks"] == 0


def test_recovery_resets_pending_counter():
    """中间一轮回到止损线上方，待确认计数必须清零（否则抖动几下就凑够了）。"""
    result = _check(8.80, 9.00, pending=1)  # mid 8.90 → -2.7%
    assert result["fire"] is False
    assert result["basis"] == "NONE"
    assert result["pending_checks"] == 0


def test_missing_bid_falls_back_to_single_basis_and_stays_conservative():
    """bid=0（无买盘）时不能因为「拿不到 mid」就放松 —— 退回单口径立即判定。"""
    result = _check(0.0, 0.0, last=7.00)

    assert result["fire"] is True
    assert result["basis"] == "MID"
    assert result["mid"] == pytest.approx(7.00)


def test_single_confirmation_restores_legacy_behaviour():
    """required_checks=1 → 等价旧行为（瞬时 bid 单点判定），供回滚/对照。"""
    result = _check(8.40, 8.80, required=1)

    assert result["fire"] is True
    assert result["basis"] == "BID"


def test_spread_guard_flags_stop_narrower_than_double_spread():
    """止损线 $73.20 < 2×价差成本 $80 → 必须打标记（这次事故的直接特征）。"""
    result = _check(8.40, 8.80)
    assert result["spread_usd"] == pytest.approx(40.0)
    assert result["spread_guard"] is True

    narrow = _check(8.40, 8.42)  # 价差 $2 → 2×=4，远小于止损线
    assert narrow["spread_guard"] is False


# ---------------------------------------------------------------------------
# 调用点守卫：以上纯函数规则必须真的接进 run_campaign，否则等于没修。
# ---------------------------------------------------------------------------

CODE = "US.COIN261120P140000"
UNDERLYING = "US.COIN"


def _load_gamma():
    import run_gamma_strategy as module
    return module


class _WideSpreadBroker:
    """价差 $0.40 的期权账户（COIN 复盘的真实盘口形态）。"""

    account_id = "281756481476169109"
    trading_env = "REAL"

    def __init__(self, bid: float, spread: float = 0.40) -> None:
        self._bid = bid
        self._spread = spread

    def positions(self) -> pd.DataFrame:
        return pd.DataFrame([{"code": CODE, "qty": 1.0, "cost_price": COST}])

    def orders(self) -> pd.DataFrame:
        return pd.DataFrame()

    def candles(self, symbol, count: int = 90, **kwargs) -> pd.DataFrame:
        bars = max(int(count), 10)
        return pd.DataFrame({
            "close": [COST * 0.9] * bars, "high": [COST * 0.95] * bars,
            "low": [COST * 0.85] * bars, "volume": [1_000_000] * bars,
        })

    def snapshots(self, symbols):
        from datetime import datetime as _dt, timedelta as _td
        from zoneinfo import ZoneInfo as _TZ

        stamp = (_dt.now(_TZ("America/New_York")) - _td(seconds=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return pd.DataFrame([{
            "code": s, "bid_price": self._bid, "ask_price": self._bid + self._spread,
            "last_price": self._bid, "update_time": stamp,
            "option_gamma": 0.01, "option_theta": 0.05,
            "option_implied_volatility": 0.30, "option_contract_multiplier": 100,
        } for s in symbols])

    def is_cancellable_status(self, status) -> bool:
        return str(status).upper() in {"SUBMITTED", "WAITING_SUBMIT", "SUBMITTING"}


def _campaign() -> dict:
    return {
        "code": CODE, "underlying": UNDERLYING, "trigger_mode": "VOLATILITY",
        "enabled": True, "exit_only": False,
        "option_type": "PUT", "structure": "LONG_OPTION", "optimizer_version": 2,
        "max_position_loss_ratio": STOP_RATIO, "max_position_loss_usd": 300.0,
        "stop_loss": 0.60,
        "legs": [{"code": CODE, "side": "BUY", "ratio": 1, "role": "PRIMARY"}],
        "entry_hours": 6, "max_hold_hours": 24,
        "contracts_per_stage": 1, "max_stages": 1, "max_premium_per_stage": 3000.0,
        "profit_target": 0.50, "trailing_stop": 0.25,
        "entry_zscore": 2.0, "minimum_underlying_move": 0.02,
        "entry_confirmation_minutes": 5,
        "entry_confirmation_min_volume_ratio": 0.80,
        "entry_confirmation_min_volume_retention": 0.65,
        "entry_confirmation_require_active": True,
        "cooldown_hours": 24, "cooldown_bypass_enabled": True,
        "event_date": "2026-10-30", "campaign_id": "cmp-coin",
    }


class _RecordingExecutor:
    def __init__(self, store: TradingStore) -> None:
        self.store = store
        self.submitted: list[dict] = []

    def submit_limit(self, strategy_id, intent_key, symbol, side, quantity, price,
                     metadata=None, **kwargs):
        self.submitted.append({"symbol": symbol, "side": side, "quantity": quantity,
                               "price": price, "metadata": metadata or {}})
        return type("R", (), {"status": "SUBMITTED", "broker_order_id": "BRK-X",
                              "message": ""})()

    @property
    def alerts(self):
        return self

    def send(self, *args, **kwargs):
        return None


def _run(rg, tmp_path, monkeypatch, state: dict, bid: float = 8.40):
    store = TradingStore(tmp_path / "coin.db")
    executor = _RecordingExecutor(store)
    monkeypatch.setattr(rg, "volatility_signal_state", lambda *a, **k: {})
    monkeypatch.setattr(rg, "sync_volatility_position_cycle", lambda *a, **k: None)
    monkeypatch.setattr(rg, "volatility_near_expiry_itm_exit_reason", lambda *a, **k: None)
    monkeypatch.setattr(
        rg.MARKET_CALENDAR, "session",
        lambda _now: type("S", (), {"regular_open": staticmethod(lambda _n: True)})(),
    )
    rg.run_campaign(
        _WideSpreadBroker(bid), executor, _campaign(), state,
        strategy_id="volatility_long_gamma",
    )
    return executor


def test_run_campaign_does_not_sell_on_single_bid_wick(tmp_path, monkeypatch):
    """端到端：一次 bid 抖动（mid 未击穿）不得提交退出单，只记待确认。"""
    rg = _load_gamma()
    state: dict = {}

    executor = _run(rg, tmp_path, monkeypatch, state, bid=8.40)

    sells = [c for c in executor.submitted if str(c["side"]).upper() == "SELL"]
    assert not sells, (
        "单次 bid 抖动就提交了退出单 —— 双轮确认没有接进 run_campaign："
        f"{executor.submitted}"
    )
    # campaign_state 是「{strategy_id}:{tenant}:{code}」键下的子字典（写形态唯一出处
    # 是 volatility_write_state_key），不能按顶层键取。
    campaign_state = state[next(iter(state))]
    assert campaign_state.get("hard_stop_pending_checks") == 1, (
        "待确认计数没有落到 campaign_state，下一轮无法完成确认"
    )


def test_run_campaign_sells_on_second_consecutive_wick(tmp_path, monkeypatch):
    """端到端：连续两轮只有 bid 在止损线下方 → 必须真的提交退出单。"""
    rg = _load_gamma()
    state: dict = {}

    # 第 1 轮：只记待确认（同时得到真实的状态键）
    _run(rg, tmp_path, monkeypatch, state, bid=8.40)
    key = next(iter(state))
    assert state[key]["hard_stop_pending_checks"] == 1

    # 第 2 轮：同一状态、同一盘口 → 必须离场
    executor = _run(rg, tmp_path, monkeypatch, state, bid=8.40)

    sells = [c for c in executor.submitted if str(c["side"]).upper() == "SELL"]
    assert len(sells) == 1, f"确认轮数够了却没有退出：{executor.submitted}"
    assert "仅可成交价" in str(sells[0]["metadata"].get("reason") or "")
    assert state[key]["hard_stop_pending_checks"] == 0, "触发后计数必须清零"
