"""ML 推理侧特征契约守卫（v3.2 阻断项）。

背景（2026-10-25 Reviewer 阻断）：v3.2 训练脚本新增了 ``moneyness`` / ``gamma_to_theta``，
但推理侧 ``prepare_symbol_features`` 只产 16 个老特征。一旦把 v3.2 模型推上生产：

    feature_df = pd.DataFrame([features])[feature_cols]   # KeyError
    except Exception: ... return 0.0                      # 被吞掉 → 全标的 0.0

即「ML 排序静默失效」，只剩一行 logger.error。本文件锁死四件事：

1. 模型 ``feature_cols`` 必须是推理可产特征集合 ``INFERENCE_FEATURE_KEYS`` 的子集
   （否则拒绝加载 → ERROR + 回退内置规则模型）；
2. 推理侧真的能产出 v3.2 那两个特征，且是真值 ``moneyness = S/K - 1``、
   ``gamma_to_theta = |gamma| / (|theta| + 1e-6)``（不是 strike 中位数 / dte·vix 的伪因子）；
3. 缺行情上下文时必须**显式报错**，不允许再静默返回 0.0；
4. 老的 v3 模型（16 特征）没有行情上下文也能正常打分 —— 平滑兼容不能被这次修复打断。

变异（必须转红）：从 ``INFERENCE_FEATURE_KEYS`` 删掉 ``moneyness``，
或把 ``load_ml_model`` 的签名校验去掉，或把 ``prepare_symbol_features`` 里
缺上下文时的行为改回填 0，本文件都必须变红。
"""

from __future__ import annotations

import logging
import pickle
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from futu_quant.ml_scorer import (  # noqa: E402
    BuiltinRuleModel,
    INFERENCE_FEATURE_KEYS,
    MissingFeatureError,
    load_ml_model,
    prepare_symbol_features,
    predict_symbol_score,
)
from futu_quant.store import TradingStore  # noqa: E402

MODEL_SYMBOL = "US.AAPL240119C175000"


@pytest.fixture(autouse=True)
def _clear_snapshot_cache():
    """快照缓存跨用例必须清掉（模块级 TTL 缓存，key 含库路径）。"""
    from futu_quant import ml_scorer

    ml_scorer._clear_snapshot_cache()
    yield
    ml_scorer._clear_snapshot_cache()
MODEL_STRIKE = 175.0

# v3.2 candidate（data/ml_models/volatility_scorer_v3_candidate.json）真实的 18 个特征。
# 推理侧少构造任何一个，上线后就是 KeyError → 0.0（静默降级）。
V32_CANDIDATE_FEATURE_COLS = [
    "days_to_expiry", "is_call", "expiry_bucket",
    "symbol_avg_pnl", "symbol_std_pnl", "symbol_max_pnl", "symbol_min_pnl",
    "symbol_win_rate", "symbol_3d_avg_pnl", "symbol_7d_avg_pnl",
    "symbol_trade_count", "symbol_pnl_trend",
    "day_of_week", "hour_of_day", "is_market_close",
    "month", "moneyness", "gamma_to_theta",
]

# 盘上生产模型（volatility_scorer_v3.pkl）的真实 16 特征：必须继续可用。
V3_PRODUCTION_FEATURE_COLS = [
    "days_to_expiry", "is_call", "strike_price", "expiry_bucket",
    "symbol_avg_pnl", "symbol_std_pnl", "symbol_max_pnl", "symbol_min_pnl",
    "symbol_win_rate", "symbol_3d_avg_pnl", "symbol_7d_avg_pnl",
    "symbol_trade_count", "symbol_pnl_trend",
    "day_of_week", "hour_of_day", "is_market_close",
]


class _DummyModel:
    """最小可用模型：只要有 ``predict_proba`` 与 ``n_jobs`` 就能被 load_ml_model 接受。"""

    n_jobs = 4

    def predict_proba(self, frame):
        import numpy as np

        return np.array([[0.4, 0.6]] * len(frame))


def _write_model(path: Path, feature_cols: list[str]) -> Path:
    with open(path, "wb") as handle:
        pickle.dump(
            {"model": _DummyModel(), "feature_cols": list(feature_cols), "version": "test"},
            handle,
        )
    return path


@pytest.mark.parametrize("feature", V32_CANDIDATE_FEATURE_COLS)
def test_every_v32_candidate_feature_is_producible(feature):
    """删掉 INFERENCE_FEATURE_KEYS 里的任意一项，对应参数化用例必须转红。"""
    assert feature in INFERENCE_FEATURE_KEYS, (
        f"v3.2 模型需要特征 {feature}，但推理侧集合没有它 —— 上线即 KeyError→0.0"
    )


def test_load_model_rejects_unproducible_feature(tmp_path, caplog):
    path = _write_model(tmp_path / "bad.pkl", ["days_to_expiry", "bogus_feature"])
    with caplog.at_level(logging.ERROR):
        model, cols = load_ml_model(str(path))

    assert isinstance(model, BuiltinRuleModel), "含不可产特征的模型必须被拒绝，回退规则模型"
    assert "bogus_feature" not in cols
    assert "bogus_feature" in caplog.text, "拒绝加载必须留 ERROR 痕，不能静默"


def test_load_model_accepts_current_production_v3_set(tmp_path):
    path = _write_model(tmp_path / "v3.pkl", V3_PRODUCTION_FEATURE_COLS)
    model, cols = load_ml_model(str(path))

    assert not isinstance(model, BuiltinRuleModel), "老 v3 模型必须照常加载（平滑兼容）"
    assert cols == V3_PRODUCTION_FEATURE_COLS


def test_prepare_symbol_features_produces_market_features(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    # moneyness 的唯一 spot 来源：最近一条 option_history 快照（不再用 last_price）
    store.import_option_history([
        {
            "code": MODEL_SYMBOL, "time_key": "2026-10-24 09:45:00",
            "underlying": "US.AAPL", "underlying_price": 200.0,
            "gamma": 0.05, "theta": -0.10, "source": "TEST",
        }
    ])
    features = prepare_symbol_features(
        MODEL_SYMBOL,
        datetime(2026, 10, 24, 10, 0),
        store,
        market_context={
            "option_gamma": 0.05,
            "option_theta": -0.10,
        },
    )

    # 两个标的级新特征需要真实行情/broker（market_context 或 broker 透传），
    # 由 tests/test_symbol_level_features_inference.py 单独覆盖；这里断言其余特征全可产。
    needs_market = {"symbol_momentum_20d", "regime_label"}
    assert (set(INFERENCE_FEATURE_KEYS) - needs_market) <= set(features), (
        f"推理侧缺特征: {sorted((set(INFERENCE_FEATURE_KEYS) - needs_market) - set(features))}"
    )
    assert features["moneyness"] == pytest.approx(200.0 / MODEL_STRIKE - 1.0)
    assert features["moneyness_lag_minutes"] == pytest.approx(15.0)
    assert features["gamma_to_theta"] == pytest.approx(0.5, rel=1e-4)
    assert features["month"] == 10
    assert features["quarter"] == 4


def test_moneyness_absent_when_snapshot_stale(tmp_path):
    """快照滞后 >45 分钟 → moneyness 置空 + 原因，**不许回落 last_price**。"""
    store = TradingStore(tmp_path / "t.db")
    store.import_option_history([
        {
            "code": MODEL_SYMBOL, "time_key": "2026-10-24 08:00:00",
            "underlying": "US.AAPL", "underlying_price": 200.0,
            "gamma": 0.05, "theta": -0.10, "source": "TEST",
        }
    ])
    features = prepare_symbol_features(
        MODEL_SYMBOL, datetime(2026, 10, 24, 10, 0), store,
        market_context={"option_gamma": 0.05, "option_theta": -0.10},
    )
    assert "moneyness" not in features
    assert features["moneyness_missing_reason"] == "snapshot_missing_or_stale"


def test_predict_raises_instead_of_silent_zero(tmp_path):
    """缺行情上下文 → 必须抛 MissingFeatureError，绝不 return 0.0（静默降级）。"""
    store = TradingStore(tmp_path / "t.db")
    with pytest.raises(MissingFeatureError):
        predict_symbol_score(
            MODEL_SYMBOL, _DummyModel(), ["moneyness", "gamma_to_theta"], store
        )


def test_legacy_v3_model_still_predicts_without_market_context(tmp_path):
    """平滑兼容：老 v3 的 16 特征不需要行情上下文，必须照常出分。"""
    store = TradingStore(tmp_path / "t.db")
    score = predict_symbol_score(
        MODEL_SYMBOL, _DummyModel(), V3_PRODUCTION_FEATURE_COLS, store
    )
    assert 0.0 <= score <= 1.0


def test_training_refuses_pseudo_moneyness():
    """训练集缺 underlying_price / 希腊字母时，训练脚本必须报错拒绝，不许换定义。"""
    pytest.importorskip("sklearn")
    import importlib

    module = importlib.import_module("scripts.ml_train_symbol_scorer_v3")
    frame = __import__("pandas").DataFrame(
        {
            "strike_price": [MODEL_STRIKE],
            "days_to_expiry": [10],
            "underlying_symbol": ["AAPL"],
            "is_profitable": [1],
            "window_net_pnl": [1.0],
        }
    )
    with pytest.raises(RuntimeError, match="underlying_price"):
        module.prepare_features(frame, feature_selection=False)


def test_training_uses_true_moneyness_and_gamma_to_theta():
    """给了真行情列以后，训练侧算出来的必须是真定义（与推理侧同一个 helper）。"""
    pytest.importorskip("sklearn")
    import importlib

    import pandas as pd

    module = importlib.import_module("scripts.ml_train_symbol_scorer_v3")
    frame = pd.DataFrame(
        {
            "strike_price": [MODEL_STRIKE, 50.0, 100.0],
            "days_to_expiry": [10, 20, 15],
            "underlying_symbol": ["AAPL", "AAPL", "AAPL"],
            "underlying_price": [200.0, 40.0, 130.0],
            "option_gamma": [0.05, 0.02, 0.06],
            "option_theta": [-0.10, -0.04, -0.03],
            "is_profitable": [1, 0, 1],
            "window_net_pnl": [100.0, -50.0, 10.0],
        }
    )
    features, _labels, _cols, _weights = module.prepare_features(
        frame, feature_selection=False
    )

    assert features["moneyness"].tolist() == pytest.approx(
        [200.0 / MODEL_STRIKE - 1.0, -0.2, 0.3]
    )
    # 第三行 gamma/theta = 0.06/0.03 = 2.0；前两行都是 0.5（+1e-6 分母）
    assert features["gamma_to_theta"].tolist() == pytest.approx([0.5, 0.5, 2.0], rel=1e-4)


def test_row_market_context_extracts_greeks_from_primary_row():
    """结构行的希腊字母在 primary_row 里时也必须取到（否则 v3.2 永远 NA）。"""
    from futu_quant.volatility_roll import _row_market_context

    row = {"primary_row": {"option_gamma": 0.05, "option_theta": -0.10}}
    assert _row_market_context(row, 200.0) == {
        "spot": 200.0,
        "option_gamma": 0.05,
        "option_theta": -0.10,
    }


def test_row_market_context_omits_missing_greeks():
    """0/NaN 的希腊字母视为缺失：不放进上下文，交给评分层显式报 NA。"""
    from futu_quant.volatility_roll import _row_market_context

    assert _row_market_context({"option_gamma": 0.0, "option_theta": float("nan")}, 100.0) == {
        "spot": 100.0
    }
