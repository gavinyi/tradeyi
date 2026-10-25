"""P0-A / P0-B 守卫：回测对照的合约级列与 fail-closed 契约。

背景（2026-10-25 Chief 更正）：回测 CSV 从来没有 `moneyness` / `gamma_to_theta`，
而 `ml_backtest_comparator.py` 的 `avail_cols` 把缺列**静默丢掉** → 大屏 ML 臂实际是
「少两个核心特征」在打分（甚至 KeyError 被吞成常数 0.50），所以之前的 🟢「满足实盘接入」
不可信。本文件锁住两件事：

- P0-A：导出必须把 `moneyness` / `gamma_to_theta` 真正写进 CSV；缺值写 NaN + 原因列，**不许填 0**；
- P0-B：比较器缺列必须 fail-closed（抛 `MissingFeatureError` 并指明缺哪列），不许静默丢弃。

变异（必须转红）：把比较器改回 `avail_cols` 过滤 → `test_comparator_fails_closed...` 红；
把导出改回「只算不写」→ `test_export_writes_contract_level_columns` 红。
"""

from __future__ import annotations

import pickle
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from futu_quant.ml_backtest_comparator import run_ml_vs_rule_comparison  # noqa: E402
from futu_quant.ml_scorer import MissingFeatureError  # noqa: E402
from scripts.ml_export_training_data_v3 import add_option_market_features  # noqa: E402

CODE = "US.AAPL240119C175000"


@pytest.fixture(autouse=True)
def _clear_snapshot_cache():
    """快照缓存是模块级 TTL 缓存（key 含库路径）：跨用例必须清掉，否则会串味。"""
    from futu_quant import ml_scorer

    ml_scorer._clear_snapshot_cache()
    yield
    ml_scorer._clear_snapshot_cache()


# ---------------------------------------------------------------- P0-A 导出写列
def _make_option_history_db(path: Path, rows: list[tuple]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE option_history ("
            "code TEXT, time_key TEXT, underlying_price REAL, gamma REAL, theta REAL)"
        )
        conn.executemany(
            "INSERT INTO option_history (code, time_key, underlying_price, gamma, theta)"
            " VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_export_writes_contract_level_columns(tmp_path):
    db = _make_option_history_db(
        tmp_path / "t.db",
        [
            (CODE, "2026-10-22 11:00:00", 200.0, 0.05, -0.10),
            (CODE, "2026-10-22 11:10:00", 210.0, 0.05, 0.0),  # theta=0 → gamma_to_theta 不可算
        ],
    )
    frame = pd.DataFrame(
        {
            "symbol": [CODE, CODE],
            "created_at": ["2026-10-22T15:05:00+00:00", "2026-10-22T15:25:00+00:00"],
            "strike_price": [175.0, 175.0],
        }
    )

    out = add_option_market_features(frame, str(db))

    assert "moneyness" in out.columns and "gamma_to_theta" in out.columns
    assert out.loc[0, "moneyness"] == pytest.approx(200.0 / 175.0 - 1.0)
    assert out.loc[0, "moneyness_lag_minutes"] == pytest.approx(5.0)
    assert out.loc[0, "gamma_to_theta"] == pytest.approx(0.5, rel=1e-4)
    assert out.loc[0, "moneyness_missing_reason"] == "OK"
    assert out.loc[0, "gamma_to_theta_missing_reason"] == "OK"

    # 第二行 theta=0 → 不可算，必须是 NaN + 原因，绝不能填 0
    assert np.isnan(out.loc[1, "gamma_to_theta"])
    assert out.loc[1, "gamma_to_theta_missing_reason"] == "zero_or_invalid_greeks"


# ---------------------------------------------------------------- P0-B 比较器 fail-closed
class _DummyModel:
    def predict_proba(self, frame):
        return np.column_stack([np.full(len(frame), 0.4), np.full(len(frame), 0.6)])


def _write_model(path: Path, feature_cols: list[str]) -> Path:
    with open(path, "wb") as handle:
        pickle.dump({"model": _DummyModel(), "feature_cols": feature_cols}, handle)
    return path


def _write_comparison_csv(path: Path, rows: int = 40, include_moneyness: bool = False) -> Path:
    frame = pd.DataFrame(
        {
            "trade_date": [
                (datetime(2026, 9, 1) + pd.Timedelta(days=index)).strftime("%Y-%m-%d")
                for index in range(rows)
            ],
            "window_net_pnl": [10.0 if index % 3 else -5.0 for index in range(rows)],
            "symbol_win_rate": [0.55] * rows,
            "symbol_avg_pnl": [1.0] * rows,
            "days_to_expiry": [10] * rows,
            "strike_price": [175.0] * rows,
        }
    )
    if include_moneyness:
        frame["moneyness"] = 0.1
    frame.to_csv(path, index=False)
    return path


def test_comparator_fails_closed_when_feature_column_missing(tmp_path):
    """模型要 moneyness，CSV 没有 → 必须抛 MissingFeatureError 并点名该列。"""
    csv_path = _write_comparison_csv(tmp_path / "data.csv", include_moneyness=False)
    model_path = _write_model(
        tmp_path / "m.pkl", ["days_to_expiry", "symbol_win_rate", "moneyness"]
    )

    with pytest.raises(MissingFeatureError, match="moneyness"):
        run_ml_vs_rule_comparison(data_path=csv_path, model_path=str(model_path))


def test_comparator_works_when_all_feature_columns_present(tmp_path):
    csv_path = _write_comparison_csv(tmp_path / "data.csv", include_moneyness=True)
    model_path = _write_model(
        tmp_path / "m.pkl", ["days_to_expiry", "symbol_win_rate", "moneyness"]
    )

    result = run_ml_vs_rule_comparison(data_path=csv_path, model_path=str(model_path))
    assert result is not None
    assert result.ml_metrics is not None
    assert result.data_quality["unusable_rows"] == 0


def test_comparator_row_level_nan_does_not_raise_but_reports(tmp_path):
    """行级缺值：不抛错（几行脏数据不许炸掉大屏），但必须计数 + 回传原因。"""
    csv_path = _write_comparison_csv(tmp_path / "data.csv", include_moneyness=True)
    frame = pd.read_csv(csv_path)
    frame.loc[0:2, "moneyness"] = np.nan  # 3 行脏数据
    frame.to_csv(csv_path, index=False)
    model_path = _write_model(
        tmp_path / "m.pkl", ["days_to_expiry", "symbol_win_rate", "moneyness"]
    )

    result = run_ml_vs_rule_comparison(data_path=csv_path, model_path=str(model_path))

    quality = result.data_quality
    assert quality["unusable_rows"] == 3
    assert quality["usable_rows"] == len(frame) - 3
    assert quality["missing_by_column"]["moneyness"] == 3


def test_export_and_inference_moneyness_are_identical(tmp_path):
    """统一口径守卫（Chief 裁定）：同一标的/同一时刻，导出侧与推理侧 moneyness 逐值相等。

    变异（推理侧改回 `owner["last_price"]`）→ 本用例必红。
    """
    from futu_quant.ml_scorer import prepare_symbol_features
    from futu_quant.store import TradingStore

    store = TradingStore(tmp_path / "trading.db")
    store.import_option_history([
        {
            "code": CODE, "time_key": "2026-10-22 11:00:00",
            "underlying": "US.AAPL", "underlying_price": 200.0,
            "gamma": 0.05, "theta": -0.10, "source": "TEST",
        }
    ])

    frame = pd.DataFrame(
        {
            "symbol": [CODE],
            "created_at": ["2026-10-22T15:05:00+00:00"],  # = 11:05 ET
            "strike_price": [175.0],
        }
    )
    exported = add_option_market_features(frame, str(tmp_path / "trading.db"))

    features = prepare_symbol_features(
        CODE, datetime(2026, 10, 22, 11, 5), store,
        market_context={"option_gamma": 0.05, "option_theta": -0.10},
    )

    assert exported.loc[0, "moneyness"] == pytest.approx(
        features["moneyness"], abs=1e-12
    ), "导出侧与推理侧 moneyness 不一致 → train/serve skew"
    assert exported.loc[0, "gamma_to_theta"] == pytest.approx(
        features["gamma_to_theta"], abs=1e-12
    ), "导出侧与推理侧 gamma_to_theta 不一致 → 同源双胞胎特征也必须统一"


def test_gamma_to_theta_uses_snapshot_not_live_context(tmp_path):
    """推理侧 gamma/theta 只取快照行；market_context 里的实时链希腊字母不再兜底。"""
    from futu_quant.ml_scorer import prepare_symbol_features
    from futu_quant.store import TradingStore

    store = TradingStore(tmp_path / "trading.db")
    store.import_option_history([
        {
            "code": CODE, "time_key": "2026-10-22 11:00:00",
            "underlying": "US.AAPL", "underlying_price": 200.0,
            "gamma": 0.05, "theta": -0.10, "source": "TEST",
        }
    ])
    features = prepare_symbol_features(
        CODE, datetime(2026, 10, 22, 11, 5), store,
        market_context={"option_gamma": 0.60, "option_theta": -0.30},  # 实时链，必须被忽略
    )
    assert features["gamma_to_theta"] == pytest.approx(0.5, rel=1e-4), (
        "用了实时链希腊字母 → 与导出侧（快照）又成了双源 skew"
    )


def test_snapshot_cache_is_isolated_between_stores(tmp_path):
    """缓存 key 必须含库标识：不同 store 的同一标的/时刻不得互相命中。"""
    from futu_quant.ml_scorer import latest_underlying_snapshot
    from futu_quant.store import TradingStore

    store_a = TradingStore(tmp_path / "a.db")
    store_a.import_option_history([
        {
            "code": CODE, "time_key": "2026-10-22 11:00:00",
            "underlying": "US.AAPL", "underlying_price": 200.0,
            "gamma": 0.05, "theta": -0.10, "source": "TEST",
        }
    ])
    store_b = TradingStore(tmp_path / "b.db")  # 空库

    first = latest_underlying_snapshot(store_a, CODE, datetime(2026, 10, 22, 11, 5))
    second = latest_underlying_snapshot(store_b, CODE, datetime(2026, 10, 22, 11, 5))

    assert first["spot"] == pytest.approx(200.0)
    assert second["spot"] is None, "不同 DB 命中了同一缓存 → 断言可能靠缓存假绿"


def test_training_excludes_missing_reason_columns():
    """导出新增的 `*_missing_reason` 是字符串，绝不能进特征列（进训会直接崩/污染）。"""
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
            "moneyness_missing_reason": ["OK"] * 4,
            "gamma_to_theta_missing_reason": ["OK"] * 4,
            "moneyness_lag_minutes": [0.5] * 4,
            "is_profitable": [1, 0, 1, 0],
            "window_net_pnl": [10.0, -5.0, 3.0, -1.0],
        }
    )

    _features, _labels, cols, _weights = module.prepare_features(
        frame, feature_selection=False
    )

    assert "moneyness_missing_reason" not in cols
    assert "gamma_to_theta_missing_reason" not in cols
    assert "moneyness_lag_minutes" not in cols, "审计列混进训练 → 模型会被 fail-closed 拒载"
