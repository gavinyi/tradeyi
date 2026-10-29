"""
模块 2: 隔离沙盒影子演练与微观特征提取器 (Sandbox Incubator Engine)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from futu_quant.store import TradingStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SimulatedSignal:
    symbol: str
    triggered_at: str
    z_score: float
    volume_ratio: float
    spread_pct: float


@dataclass(frozen=True)
class SimulatedTrade:
    symbol: str
    is_win: bool
    pnl_usd: float
    return_ratio: float
    slippage_pct: float
    volume_retention: float


class IncubatorEngine:
    """隔离沙盒影子演练与微观特征提取器"""

    def __init__(self, store: TradingStore | None = None):
        self.store = store or TradingStore()

    def record_simulated_signal(
        self,
        symbol: str,
        z_score: float,
        volume_ratio: float,
        spread_pct: float,
        triggered_at: str | None = None,
    ) -> None:
        """记录沙盒中的虚拟突破信号，不触发券商下单。"""
        now = triggered_at or datetime.now(timezone.utc).isoformat()
        current = self.store.incubator_symbol(symbol) or {
            "symbol": symbol,
            "first_observed_at": now,
            "observation_days": 1,
            "signal_triggers": 0,
            "simulated_wins": 0,
            "simulated_losses": 0,
            "simulated_pnl_usd": 0.0,
            "simulated_max_dd": 0.0,
            "avg_slippage_pct": spread_pct,
            "volume_retention_ratio": 0.70,
            "latest_ml_score": 0.50,
            "promotion_eligible": 0,
        }
        current["signal_triggers"] = int(current.get("signal_triggers", 0)) + 1
        current["avg_slippage_pct"] = (
            float(current.get("avg_slippage_pct", spread_pct)) * 0.8 + spread_pct * 0.2
        )
        self.store.upsert_incubator_metrics(symbol, current)

    def open_virtual_position(
        self, symbol: str, entry_price: float, signal_key: str, opened_at: str | None = None,
        holding_hours: int = 24,
    ) -> bool:
        """按真实快照价幂等建立本地虚拟仓；不经过任何券商下单接口。"""
        if entry_price <= 0:
            return False
        opened = datetime.fromisoformat(opened_at) if opened_at else datetime.now(timezone.utc)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        created = self.store.open_incubator_virtual_position(
            symbol=symbol,
            signal_key=signal_key,
            entry_price=entry_price,
            opened_at=opened.isoformat(),
            settle_after=(opened + timedelta(hours=holding_hours)).isoformat(),
        )
        if created:
            self.store.record_universe_lifecycle_event(
                "VIRTUAL_POSITION_OPENED", symbol,
                {"signal_key": signal_key, "entry_price": entry_price},
            )
        return created

    def settle_due_positions(
        self, prices: dict[str, float], settled_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """按最新真实行情幂等结算到期虚拟仓并回写赛马指标。"""
        now = settled_at or datetime.now(timezone.utc).isoformat()
        settled: list[dict[str, Any]] = []
        for position in self.store.open_incubator_virtual_positions(due_at=now):
            exit_price = float(prices.get(str(position["symbol"]), 0.0) or 0.0)
            if exit_price <= 0:
                self.store.record_universe_lifecycle_event(
                    "VIRTUAL_SETTLEMENT_DEFERRED_NO_QUOTE", str(position["symbol"]),
                    {"position_id": position["id"], "reason": "缺少有效结算行情，保持 OPEN"},
                )
                continue
            result = self.store.settle_incubator_virtual_position(position["id"], exit_price, now)
            if result is None:
                continue
            metrics = result["metrics"]
            self.store.record_universe_lifecycle_event(
                "VIRTUAL_POSITION_SETTLED", str(result["symbol"]),
                {"position_id": result["id"], "pnl_usd": result["pnl_usd"],
                 "return_ratio": result["return_ratio"]},
            )
            result["metrics"] = metrics
            settled.append(result)
        return settled

    def update_simulated_trade(
        self,
        symbol: str,
        is_win: bool,
        pnl_usd: float,
        return_ratio: float,
        slippage_pct: float,
        volume_retention: float,
    ) -> dict[str, Any]:
        """记录沙盒模拟平仓，聚合胜率、累计盈亏与动态最大回撤。"""
        now = datetime.now(timezone.utc).isoformat()
        current = self.store.incubator_symbol(symbol) or {
            "symbol": symbol,
            "first_observed_at": now,
            "observation_days": 1,
            "signal_triggers": 1,
            "simulated_wins": 0,
            "simulated_losses": 0,
            "simulated_pnl_usd": 0.0,
            "simulated_max_dd": 0.0,
            "avg_slippage_pct": slippage_pct,
            "volume_retention_ratio": volume_retention,
            "latest_ml_score": 0.50,
            "promotion_eligible": 0,
        }

        if is_win:
            current["simulated_wins"] = int(current.get("simulated_wins", 0)) + 1
        else:
            current["simulated_losses"] = int(current.get("simulated_losses", 0)) + 1

        prev_pnl = float(current.get("simulated_pnl_usd", 0.0))
        new_pnl = prev_pnl + pnl_usd
        current["simulated_pnl_usd"] = new_pnl

        # 滑点与留存率指数平滑更新
        current["avg_slippage_pct"] = (
            float(current.get("avg_slippage_pct", 0.0)) * 0.7 + slippage_pct * 0.3
        )
        current["volume_retention_ratio"] = (
            float(current.get("volume_retention_ratio", 0.70)) * 0.7 + volume_retention * 0.3
        )

        # 动态虚拟回撤测算 (相对于基准虚拟资金池 5000 美元)
        capital_base = 5000.0
        peak_pnl = max(0.0, prev_pnl, new_pnl)
        if peak_pnl > new_pnl:
            drawdown = (peak_pnl - new_pnl) / (capital_base + peak_pnl)
            current["simulated_max_dd"] = max(
                float(current.get("simulated_max_dd", 0.0)), drawdown
            )

        self.store.upsert_incubator_metrics(symbol, current)
        return current

    def evaluate_daily_metrics(
        self, symbol: str, current_days: int | None = None
    ) -> bool:
        """每日盘后评估标的是否满足晋级硬门槛。"""
        current = self.store.incubator_symbol(symbol)
        if not current:
            return False

        if current_days is not None:
            obs_days = current_days
        else:
            first_at = current.get("first_observed_at")
            if first_at:
                try:
                    f_date = datetime.fromisoformat(str(first_at)).date()
                    now_date = datetime.now(timezone.utc).date()
                    obs_days = max(1, (now_date - f_date).days + 1)
                except Exception:
                    obs_days = max(1, int(current.get("observation_days", 1)) + 1)
            else:
                obs_days = max(1, int(current.get("observation_days", 1)) + 1)
        signals = int(current.get("signal_triggers", 0))
        pnl = float(current.get("simulated_pnl_usd", 0.0))
        max_dd = float(current.get("simulated_max_dd", 0.0))
        slippage = float(current.get("avg_slippage_pct", 0.0))
        retention = float(current.get("volume_retention_ratio", 0.0))
        ml_score = float(current.get("latest_ml_score", 0.0))

        # -------------------------------------------------------------
        # 双轨准入评估体系 (Dual-Track Promotion System):
        # -------------------------------------------------------------
        # A. 暴风快轨 (Fast-Track / 3日极速转正): 专为阶段性大黑马主升浪设计
        #    • 观察天数仅需 >= 3 个交易日 (绝不死等 10 天导致错失黄金期)
        #    • 突破信号 >= 2 次，平值期权价差极度紧密 <= 2.5%
        #    • 虚拟净收益为正且最大回撤严格 < 15%
        #    • 最新 ML 得分达到极高爆发分 (>= 0.82)
        fast_track_eligible = (
            obs_days >= 3
            and signals >= 2
            and pnl >= 0.0
            and max_dd < 0.15
            and slippage <= 0.025
            and retention >= 0.65
            and ml_score >= 0.82
        )

        # B. 常规慢轨 (Standard Track / 7~10日稳健考察): 专为中长线稳健白马设计
        #    • 观察天数满 7 个交易日 (滑动 1.5 ~ 2 周) 且信号 >= 3 次
        #    • 虚拟净收益 > 0，最大回撤 < 25%，滑点 <= 4.0%，ML 得分 >= 0.75
        standard_eligible = (
            obs_days >= 7
            and signals >= 3
            and pnl >= 0.0
            and max_dd < 0.25
            and slippage <= 0.040
            and retention >= 0.60
            and ml_score >= 0.75
        )

        eligible = fast_track_eligible or standard_eligible
        track_label = "FAST_TRACK" if fast_track_eligible else "STANDARD" if standard_eligible else "INCUBATING"

        current["observation_days"] = obs_days
        current["promotion_eligible"] = 1 if eligible else 0
        current["track_mode"] = track_label
        self.store.upsert_incubator_metrics(symbol, current)
        return eligible
