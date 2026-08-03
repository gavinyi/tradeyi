from __future__ import annotations

from dataclasses import replace

import pandas as pd

from futu_quant.crypto_swap import CryptoSwapConfig, CryptoSwapDecision
from futu_quant.crypto_swap_radar import (
    managed_exit_decision,
    rank_swap_candidate,
    scan_crypto_swap_radar,
)
from futu_quant.crypto_swap_watchlist import normalize_swap_symbols


def test_swap_symbols_are_normalized_and_deduplicated():
    assert normalize_swap_symbols([
        "btc-usdt-swap", "BTC-USDT-SWAP", "eth-usdt-swap",
    ]) == (
        "BTC-USDT-SWAP", "ETH-USDT-SWAP",
    )


def test_candidate_ranking_blocks_expensive_funding_and_wide_spread():
    decision = CryptoSwapDecision(
        "OPEN_LONG", "信号通过",
        metrics={"volatility_expansion": 2, "zscore": 3, "volume_ratio": 2},
    )
    row = rank_swap_candidate(
        decision,
        {"instId": "BTC-USDT-SWAP", "bidPx": "99", "askPx": "101"},
        {"fundingRate": "0.002"}, {"oi": "10000"},
        max_abs_funding_rate=.001, max_spread_ratio=.005,
    )
    assert row["eligible"] is False
    assert "资金费率" in row["reason"]
    assert "盘口价差" in row["reason"]


def test_managed_candidate_ranking_uses_managed_short_score_and_checks():
    decision = CryptoSwapDecision(
        "HOLD", "弱势评分不足",
        metrics={
            "managed_short_score": 70,
            "relative_weakness": -0.04,
            "volume_ratio": 1.2,
        },
        checks=[
            {
                "name": "综合评分",
                "current": "70",
                "target": "≥ 80",
                "passed": False,
            }
        ],
    )
    row = rank_swap_candidate(
        decision,
        {"instId": "DOGE-USDT-SWAP", "bidPx": "99.9", "askPx": "100.0"},
        {"fundingRate": "0.0001"}, {"oi": "10000"},
        max_abs_funding_rate=.001, max_spread_ratio=.005,
    )
    assert row["eligible"] is False
    assert row["score"] > 60
    assert row["managed_short_score"] == 70
    assert "综合评分未通过" in row["reason"]


class RadarBroker:
    def candles(self, symbol, bar="1H", limit=300):
        return pd.DataFrame({"close": [1.0] * 80, "volume": [1.0] * 80})

    def ticker(self, symbol):
        return {"instId": symbol, "bidPx": "99.9", "askPx": "100.0"}

    def funding_rate(self, symbol):
        return {"fundingRate": "0.0001"}

    def open_interest(self, symbol):
        return {"oi": "12000"}


def test_radar_promotes_only_ranked_eligible_candidates(monkeypatch):
    monkeypatch.setenv("CRYPTO_SWAP_ENGINE", "LEGACY_VOLATILITY")
    monkeypatch.setattr(
        "futu_quant.crypto_swap_radar.decide_crypto_swap",
        lambda *args, **kwargs: CryptoSwapDecision(
            "OPEN_SHORT", "信号通过",
            metrics={"volatility_expansion": 2, "zscore": -3, "volume_ratio": 2},
        ),
    )
    added = []
    rows = scan_crypto_swap_radar(
        RadarBroker(), ["SOL-USDT-SWAP"], ["BTC-USDT-SWAP"],
        replace(CryptoSwapConfig(), slow_window=72),
        auto_add=True, add_symbol=added.append,
    )
    assert rows[0]["eligible"] is True
    assert rows[0]["added"] is True
    assert added == ["SOL-USDT-SWAP"]


def test_radar_full_scan_scores_pool_before_auto_add(monkeypatch):
    monkeypatch.setenv("CRYPTO_SWAP_ENGINE", "LEGACY_VOLATILITY")
    monkeypatch.setattr(
        "futu_quant.crypto_swap_radar.decide_crypto_swap",
        lambda *args, **kwargs: CryptoSwapDecision(
            "OPEN_SHORT", "信号通过",
            metrics={"volatility_expansion": 2, "zscore": -3, "volume_ratio": 2},
        ),
    )
    added = []
    rows = scan_crypto_swap_radar(
        RadarBroker(), ["SOL-USDT-SWAP", "DOGE-USDT-SWAP"], ["BTC-USDT-SWAP"],
        replace(CryptoSwapConfig(), slow_window=72),
        max_symbols=1,
        full_scan=True,
        auto_add=True,
        add_symbol=added.append,
        min_auto_add_score=60,
    )
    assert [row["symbol"] for row in rows] == ["SOL-USDT-SWAP", "DOGE-USDT-SWAP"]
    assert len(added) == 1


def test_managed_symbol_is_not_removed_while_position_exists():
    cycles, remove, reason = managed_exit_decision(
        has_position=True, has_pending_order=False, action="HOLD",
        inactive_cycles=99, exit_after_cycles=2,
    )
    assert cycles == 0
    assert remove is False
    assert "持仓" in reason
