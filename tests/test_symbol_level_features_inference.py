"""标的级新特征（动量 / 大盘方向 / regime 标签）推理链与导出守卫。

背景：Analyst 实测「只用 10 个合约级特征折内重训 OOT AUC 0.5346≈随机，全 19 特征 0.6758」
→ 判别力几乎全来自 `symbol_*` 标的级历史特征；补标的级新信息是唯一增量方向。
另：`volatility_roll._apply_ml_ranking` 曾传 `broker=None`，导致
`vix_level/spy_return_1d/5d/market_regime` 在排名链路里全是常量占位（20/0/0/1）——
本文件把「传入 broker/行情后必须拿到真实值」变成可失败断言。

变异（必须转红）：
- 把 `prepare_symbol_features` 的 broker 透传删掉 → broker 用例红；
- 把 `momentum_20d_from_bars` 改成包含当根（`close[-1]`）→ 防 look-ahead 用例红；
- 把导出里的 `session_date < trade_day` 改成 `<=` → 防 look-ahead 用例红。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from futu_quant.ml_scorer import (  # noqa: E402
    INFERENCE_FEATURE_KEYS,
    MissingFeatureError,
    momentum_20d_from_bars,
    predict_symbol_score,
    prepare_symbol_features,
    regime_label_from_spy,
)
from futu_quant.store import TradingStore  # noqa: E402

SYMBOL = "US.AAPL240119C175000"
TRADE_TIME = datetime(2026, 10, 22, 10, 0)


def _business_days(count: int, end: datetime) -> list:
    days = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


class _BrokerStub:
    """最小 broker：只提供 market_env 与 candles 需要的方法。"""

    def __init__(self, closes: list[float], spy_closes: list[float], vix: float = 25.0):
        days = _business_days(len(closes), datetime(2026, 10, 21))
        self._closes_frame = pd.DataFrame(
            {"time_key": [day.strftime("%Y-%m-%d") for day in days], "close": closes}
        )
        self._spy = pd.DataFrame(
            {
                "time_key": [day.strftime("%Y-%m-%d") for day in days],
                "close": spy_closes,
            }
        )
        self._vix = vix

    def quote_snapshot(self, codes):
        return [{"code": codes[0], "last_price": self._vix}]

    def get_history_kline(self, symbol, start, end, ktype="K_DAY"):
        return self._spy[["time_key", "close"]].to_dict("records")

    def candles(self, symbol, count=120, bar=None):
        return self._closes_frame.copy()


def _rising_closes(count: int = 30, start: float = 100.0, step: float = 1.0) -> list[float]:
    return [start + step * index for index in range(count)]


def test_inference_contract_includes_new_symbol_level_keys():
    for key in ("symbol_momentum_20d", "regime_label", "spy_return_5d"):
        assert key in INFERENCE_FEATURE_KEYS


def test_broker_passthrough_gives_real_market_environment(tmp_path):
    """broker 传入后 vix/spy/regime 不得再是常量占位 20/0/0/1。"""
    store = TradingStore(tmp_path / "t.db")
    broker = _BrokerStub(
        closes=_rising_closes(), spy_closes=_rising_closes(count=30, start=400.0, step=2.0),
        vix=25.0,
    )

    features = prepare_symbol_features(SYMBOL, TRADE_TIME, store, broker=broker)

    assert features["vix_level"] == pytest.approx(25.0)
    assert features["spy_return_5d"] != 0.0, "spy_return_5d 仍是占位 0 → broker 透传失效"
    assert features["market_regime"] != 1 or features["spy_return_5d"] != 0.0
    # 动量：close[-2]/close[-22]-1 = (100+28)/(100+8)-1
    assert features["symbol_momentum_20d"] == pytest.approx(128.0 / 108.0 - 1.0)
    # 趋势标签：SPY 5 日明显上涨 → +1
    assert features["regime_label"] == 1


def test_without_broker_market_features_stay_placeholder_and_momentum_absent(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    features = prepare_symbol_features(SYMBOL, TRADE_TIME, store)

    assert features["vix_level"] == 20.0
    assert features["spy_return_1d"] == 0.0
    assert features["market_regime"] == 1
    assert "symbol_momentum_20d" not in features, "没行情时不得编动量 0"
    assert "regime_label" not in features


def test_market_context_momentum_is_used_without_broker(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    features = prepare_symbol_features(
        SYMBOL, TRADE_TIME, store, market_context={"symbol_momentum_20d": 0.123}
    )
    assert features["symbol_momentum_20d"] == pytest.approx(0.123)


def test_model_requiring_new_features_fails_loudly_without_broker(tmp_path):
    """模型需要动量/regime 但拿不到 → 抛 MissingFeatureError（不许静默 0.0）。"""

    class _Dummy:
        def predict_proba(self, frame):
            return np.array([[0.4, 0.6]] * len(frame))

    store = TradingStore(tmp_path / "t.db")
    with pytest.raises(MissingFeatureError):
        predict_symbol_score(
            SYMBOL, _Dummy(), ["symbol_momentum_20d", "regime_label"], store
        )


def test_regime_label_mapping():
    assert regime_label_from_spy(0.02) == 1
    assert regime_label_from_spy(-0.02) == -1
    assert regime_label_from_spy(0.001) == 0
    assert regime_label_from_spy(None) is None
    assert regime_label_from_spy(float("nan")) is None


def test_momentum_uses_only_completed_bars():
    """实时口径：最后一根可能是未收盘当根 → 用 close[-2]，当根暴涨不影响动量。"""
    closes = [100.0] * 20 + [110.0, 999.0]  # 倒数第 2 根 110，最后一根当根异常 999
    frame = pd.DataFrame({"close": closes})
    assert momentum_20d_from_bars(frame) == pytest.approx(110.0 / 100.0 - 1.0)


def _write_bars(warehouse: Path, symbol: str, days: list[datetime], closes: list[float]) -> None:
    target = warehouse / "bars" / f"symbol={symbol.replace('.', '_')}" / "year=2026"
    target.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "session_date": [day.strftime("%Y-%m-%d") for day in days],
            "close": closes,
        }
    ).to_parquet(target / "part.parquet", index=False)


def test_export_symbol_level_features_no_lookahead(tmp_path):
    """导出侧：动量必须只用成交日之前的日线；成交当日暴涨不得进入特征。"""
    from scripts.ml_export_training_data_v3 import add_symbol_level_features

    warehouse = tmp_path / "warehouse"
    days = _business_days(30, datetime(2026, 10, 22))
    closes = [100.0 + index for index in range(29)] + [999.0]  # 最后一天（成交日）异常
    _write_bars(warehouse, "US.AAPL", days, closes)
    _write_bars(warehouse, "US.SPY", days, [100.0 + index for index in range(30)])

    frame = pd.DataFrame(
        {
            "symbol": [SYMBOL],
            "trade_date": [days[-1].strftime("%Y-%m-%d")],
            "spy_return_5d": [0.0],
        }
    )
    out = add_symbol_level_features(frame, warehouse)

    # 截断到成交日之前：最后一条是 128.0，20 日前是 108.0
    assert out.loc[0, "symbol_momentum_20d"] == pytest.approx(128.0 / 108.0 - 1.0)
    assert out.loc[0, "spy_return_5d"] != 0.0
    assert out.loc[0, "regime_label"] == 1


def test_export_missing_underlying_keeps_nan(tmp_path):
    from scripts.ml_export_training_data_v3 import add_symbol_level_features

    warehouse = tmp_path / "warehouse"
    days = _business_days(30, datetime(2026, 10, 22))
    _write_bars(warehouse, "US.SPY", days, [100.0 + index for index in range(30)])
    frame = pd.DataFrame(
        {"symbol": [SYMBOL], "trade_date": [days[-1].strftime("%Y-%m-%d")], "spy_return_5d": [0.0]}
    )
    out = add_symbol_level_features(frame, warehouse)
    assert pd.isna(out.loc[0, "symbol_momentum_20d_observation"]) or pd.isna(
        out.loc[0, "symbol_momentum_20d"]
    ), "拿不到标的日线时必须 NaN，不能编 0"


def test_momentum_train_and_serve_implementations_agree():
    """Reviewer 钉死：同一标的/同一交易日/同一时刻，实时侧与回补侧必须逐值相等。

    实时侧 `close[-2]/close[-22]`（最后一根是当日未收盘）；回补侧先用
    `session_date < trade_date` 截断到成交日前，再 `close[-1]/close[-21]`。
    两条口径若漂移，就是 moneyness 那类 train/serve skew（阻断项）。
    """
    bars = pd.DataFrame({"close": [100.0 + index for index in range(30)] + [999.0]})

    serve = momentum_20d_from_bars(bars)
    train = momentum_20d_from_bars(bars.iloc[:-1], include_last=True)

    assert serve is not None and train is not None
    assert serve == pytest.approx(train, abs=1e-12)


def test_momentum_is_scale_invariant_between_implementations():
    """随机收盘序列也逐值相等（不是只有等差数列凑巧对齐）。"""
    import random

    random.seed(7)
    closes = [round(50 + random.random() * 100, 4) for _ in range(40)]
    bars = pd.DataFrame({"close": closes})

    serve = momentum_20d_from_bars(bars)
    train = momentum_20d_from_bars(bars.iloc[:-1], include_last=True)

    assert serve == pytest.approx(train, abs=1e-12)


def test_observation_column_never_enters_inference_contract():
    """「只观测」列不得进推理契约——否则"只观测"会悄悄变成"模型多学一列"。"""
    assert "symbol_momentum_20d_observation" not in INFERENCE_FEATURE_KEYS
    assert "symbol_momentum_missing_reason" not in INFERENCE_FEATURE_KEYS


def test_observation_columns_are_excluded_from_training_features():
    """训练侧：`*_observation` 列与缺失原因列必须被排除（变异：塞进去必红）。"""
    pytest.importorskip("sklearn")
    import importlib

    module = importlib.import_module("scripts.ml_train_symbol_scorer_v3")
    frame = pd.DataFrame(
        {
            "symbol_win_rate": [0.5, 0.6, 0.4, 0.7],
            "days_to_expiry": [10, 12, 9, 15],
            "strike_price": [100.0, 110.0, 95.0, 120.0],
            "underlying_price": [105.0, 115.0, 90.0, 125.0],
            "option_gamma": [0.05, 0.04, 0.06, 0.03],
            "option_theta": [-0.10, -0.09, -0.12, -0.08],
            "symbol_momentum_20d_observation": [0.1, float("nan"), -0.2, 0.3],
            "symbol_momentum_missing_reason": ["OK", "insufficient_history", "OK", "OK"],
            "is_profitable": [1, 0, 1, 0],
            "window_net_pnl": [10.0, -5.0, 3.0, -1.0],
        }
    )

    features, _labels, cols, _weights = module.prepare_features(
        frame, feature_selection=False
    )

    assert "symbol_momentum_20d_observation" not in cols
    assert "symbol_momentum_missing_reason" not in cols
    assert "symbol_momentum_20d_observation" not in features.columns


def _write_option_history_db(path: Path, rows: list[tuple]) -> Path:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE option_history ("
            "code TEXT, time_key TEXT, underlying TEXT, underlying_price REAL)"
        )
        conn.executemany(
            "INSERT INTO option_history (code, time_key, underlying, underlying_price)"
            " VALUES (?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_export_uses_option_history_fallback_for_momentum(tmp_path):
    """日线仓库缺标的时，用 option_history.underlying_price 派生 20 日动量兜底。"""
    from scripts.ml_export_training_data_v3 import add_symbol_level_features

    warehouse = tmp_path / "warehouse"
    days = _business_days(30, datetime(2026, 10, 22))
    _write_bars(warehouse, "US.SPY", days, [100.0 + index for index in range(30)])

    rows = []
    for index, day in enumerate(days[:-1]):  # 只到成交日前一天
        rows.append((SYMBOL, f"{day.strftime('%Y-%m-%d')} 15:59:00", "US.AAPL", 100.0 + index))
    db = _write_option_history_db(tmp_path / "t.db", rows)

    frame = pd.DataFrame(
        {"symbol": [SYMBOL], "trade_date": [days[-1].strftime("%Y-%m-%d")], "spy_return_5d": [0.0]}
    )
    out = add_symbol_level_features(frame, warehouse, db)

    assert out.loc[0, "symbol_momentum_20d"] == pytest.approx(128.0 / 108.0 - 1.0)
    assert out.loc[0, "symbol_momentum_missing_reason"] == "OK_FALLBACK"


def test_export_low_coverage_momentum_downgraded_to_observation(tmp_path):
    """覆盖率 <60% → 动量改列名只留观测（缺失非随机，硬灌会把缺失模式学成信号）。"""
    from scripts.ml_export_training_data_v3 import add_symbol_level_features

    warehouse = tmp_path / "warehouse"
    days = _business_days(30, datetime(2026, 10, 22))
    _write_bars(warehouse, "US.SPY", days, [100.0 + index for index in range(30)])
    frame = pd.DataFrame(
        {
            "symbol": [SYMBOL, "US.NVDA240119C100000"],
            "trade_date": [days[-1].strftime("%Y-%m-%d")] * 2,
            "spy_return_5d": [0.0, 0.0],
        }
    )
    out = add_symbol_level_features(frame, warehouse)

    assert "symbol_momentum_20d" not in out.columns
    assert "symbol_momentum_20d_observation" in out.columns
    # 缺失原因必须落列（缺失非随机，将来判断能否入训靠它，不能只打日志）
    assert set(out["symbol_momentum_missing_reason"]) == {"no_bars_symbol"}
