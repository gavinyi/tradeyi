"""ML 模型健康守卫。

这些判据存在的理由：2026-10-23 之前，ML 静默降级了 6 周没有任何人知道。
判据必须**能失败** —— 每条都对应一种「守卫看着在、其实永远绿」的退化方式。
"""

from __future__ import annotations

import pickle
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from futu_quant import ml_freshness as mf

# ML 1a（新鲜度守卫）当前是**未过闸草稿**（P1-A 冻结，用户 2026-10-23 决定）。
# 草稿实现与草稿测试尚未对齐，且该功能未签字、不部署、不计完成 ——
# 因此本文件在隔离环境的回归基线中**跳过**，避免"草稿红"污染隔离验收。
# 解除条件：P1-A 过闸（PRD + 设计 + Chief 签字 + 计划）后去掉此 skip 并修绿。
# P2-1a 已过闸（PRD+设计已有，Chief 签署见群）—— 草稿解除跳过，进入回归基线。
# 判据必须真实跑起来：跳过过的判据等于没有判据（本文件此前整体 skip，掩盖了
# `model_age_days` 读 json 的时间优先级错误）。


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _write_dict_pickle(path: Path, trained_at: str) -> Path:
    """写一个只含 dict 的 pkl —— 不需要 sklearn 也能 unpickle。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        pickle.dump(
            {"model": object(), "feature_cols": ["a", "b"], "trained_at": trained_at},
            handle,
        )
    return path


def _write_unpicklable(path: Path) -> Path:
    """写一个**反序列化必然失败**的 pkl。

    做法：把类定义在一个临时模块文件里，dump 后删掉文件并摘掉 sys.modules 条目。
    unpickle 时会去 import 那个模块 → ModuleNotFoundError，与「sklearn 未安装 →
    pickle.load 抛 ModuleNotFoundError」完全同形态。
    """
    import importlib

    path.parent.mkdir(parents=True, exist_ok=True)
    mod_dir = path.parent / "_missing_dep"
    mod_dir.mkdir(exist_ok=True)
    mod_file = mod_dir / "_ml_missing_dep.py"
    mod_file.write_text("class Boom:\n    pass\n", encoding="utf-8")
    sys.path.insert(0, str(mod_dir))
    try:
        module = importlib.import_module("_ml_missing_dep")
        with open(path, "wb") as handle:
            pickle.dump({"model": module.Boom(), "feature_cols": ["a"]}, handle)
    finally:
        sys.path.remove(str(mod_dir))
        sys.modules.pop("_ml_missing_dep", None)
        importlib.invalidate_caches()
        mod_file.unlink(missing_ok=True)
    return path


# --------------------------------------------------------------------------
# ① 年龄来源：必须是 pkl，绝不是元数据 json
# --------------------------------------------------------------------------


def test_age_comes_from_pkl_trained_at_not_metadata_json(tmp_path):
    """元数据 json 的 trained_at 是「刷新时间」不是「训练时间」—— 用它当判据等于没有判据。"""
    model = _write_dict_pickle(tmp_path / "volatility_scorer_v3.pkl", "2020-01-01T00:00:00+00:00")
    # 故意把 json 写成「刚刚刷新」—— 陷阱脚本 run_monthly_ml_retrain.py 就是这么干的
    model.with_suffix(".json").write_text(
        '{"trained_at": "%s"}' % datetime.now(timezone.utc).isoformat(), encoding="utf-8"
    )

    age_days, source = mf.model_age_days(model)

    assert source == "pkl.trained_at", "年龄来源必须是 pkl 本体，不能是元数据 json"
    assert age_days is not None and age_days > 2000, "必须读出 pkl 里的真实训练时间"
    health = mf.ml_model_health(model_path=model)
    assert health["age_source"] == "pkl.trained_at"
    assert health["freshness"] == mf.FRESHNESS_STALE
    # json 的值仍然单独展示，且不被当作年龄
    assert health["metadata_json_trained_at"] is not None
    assert health["trained_at"].startswith("2020-01-01")


def test_age_falls_back_to_mtime_when_pickle_unreadable(tmp_path):
    """缺 sklearn 时 unpickle 必然失败 —— 此时必须回落文件 mtime，且状态可读。"""
    model = _write_unpicklable(tmp_path / "volatility_scorer_v3.pkl")

    age_days, source = mf.model_age_days(model)

    assert source == "pkl.mtime", "pkl 读不出时必须回落 mtime，不能返回 unknown"
    assert age_days is not None and 0 <= age_days < 1


def test_missing_model_is_unknown_and_never_raises(tmp_path):
    missing = tmp_path / "nope.pkl"
    age_days, source = mf.model_age_days(missing)
    assert (age_days, source) == (None, "unknown")

    health = mf.ml_model_health(model_path=missing)
    assert health["model_exists"] is False
    assert health["freshness"] == mf.FRESHNESS_UNKNOWN


# --------------------------------------------------------------------------
# ② 新鲜度分档
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age,expected",
    [
        (0, mf.FRESHNESS_OK),
        (29.9, mf.FRESHNESS_OK),
        (30, mf.FRESHNESS_WARN),
        (34.9, mf.FRESHNESS_WARN),
        (35, mf.FRESHNESS_STALE),
        (400, mf.FRESHNESS_STALE),
        (None, mf.FRESHNESS_UNKNOWN),
    ],
)
def test_freshness_thresholds(age, expected):
    assert mf.classify_freshness(age) == expected


def test_invalid_thresholds_raise():
    """阈值写反必须抛 —— 静默的非法配置会永远返回同一档。"""
    with pytest.raises(ValueError):
        mf.classify_freshness(10, warn_days=60, stale_days=35)


# --------------------------------------------------------------------------
# ③ 打分方式：把「显式禁用」和「静默降级」分开
# --------------------------------------------------------------------------


def test_disabled_is_not_reported_as_fallback(tmp_path):
    """ML_ENABLED=false 是配置选择，不是故障 —— 两者混为一谈会制造假告警。"""
    model = _write_dict_pickle(tmp_path / "m.pkl", "2020-01-01T00:00:00+00:00")

    result = mf.detect_scoring_mode(model, enabled=False)

    assert result["mode"] == mf.SCORING_DISABLED
    assert result["degradation_cause"] == ""


def test_rule_fallback_detected_when_dependency_missing(tmp_path, monkeypatch):
    """缺 sklearn → 回退规则打分。这是本次事故的真实形态。"""
    model = _write_unpicklable(tmp_path / "m.pkl")
    monkeypatch.setattr(mf.importlib.util, "find_spec", lambda _name: None)

    result = mf.detect_scoring_mode(model)

    assert result["mode"] == mf.SCORING_RULE_FALLBACK
    assert result["sklearn_available"] is False
    assert result["degradation_cause"] == "sklearn_missing"


def test_rule_fallback_detected_when_dependency_present_but_unpicklable(tmp_path, monkeypatch):
    """依赖在、但模型读不出来 —— 归因必须与「缺依赖」区分开，否则会修错方向。"""
    model = _write_unpicklable(tmp_path / "m.pkl")
    monkeypatch.setattr(mf.importlib.util, "find_spec", lambda _name: object())

    result = mf.detect_scoring_mode(model)

    assert result["mode"] == mf.SCORING_RULE_FALLBACK
    assert result["degradation_cause"] == "model_unpickle_failed"


def test_ml_mode_when_model_loads(tmp_path, monkeypatch):
    """能加载出真模型时必须是 ml，不能因为保守而误报降级。"""
    model = _write_dict_pickle(tmp_path / "m.pkl", "2020-01-01T00:00:00+00:00")

    class FakeScorer:
        def __init__(self, **_kwargs):
            self.enabled = True
            self.model = object()  # 不是 BuiltinRuleModel

    monkeypatch.setattr("futu_quant.ml_symbol_scorer.MLSymbolScorer", FakeScorer)
    result = mf.detect_scoring_mode(model)

    assert result["mode"] == mf.SCORING_ML
    assert result["model_loaded"] is True


def test_health_reports_both_age_and_scoring_mode(tmp_path):
    model = _write_dict_pickle(tmp_path / "m.pkl", "2020-01-01T00:00:00+00:00")
    health = mf.ml_model_health(model_path=model)
    assert {"age_days", "age_source", "freshness", "scoring_mode", "scoring_label"} <= set(health)


# --------------------------------------------------------------------------
# ④ 上报与投递校验
# --------------------------------------------------------------------------


class _FakeAlerts:
    def __init__(self):
        self.sent = []

    def send(self, severity, event_type, message, incident_key=None):
        self.sent.append((severity, event_type, message, incident_key))


class _FakeStore:
    def __init__(self, delivered=1):
        self.delivered = delivered
        self.rows = []

    def alerts(self, limit=200):
        return self.rows


def test_guard_raises_stale_and_degraded(tmp_path):
    model = _write_dict_pickle(tmp_path / "m.pkl", "2020-01-01T00:00:00+00:00")
    alerts = _FakeAlerts()
    store = _FakeStore()

    mf.check_ml_model_health(store, alerts, model_path=model)

    event_types = [event for _sev, event, _msg, _key in alerts.sent]
    assert mf.EVENT_STALE in event_types


def test_guard_escalates_when_alert_not_delivered(tmp_path):
    """只写库不推送 = 「库里有、用户看不到」。守卫必须自己发现这件事。"""
    model = _write_dict_pickle(tmp_path / "m.pkl", "2020-01-01T00:00:00+00:00")
    alerts = _FakeAlerts()
    store = _FakeStore(delivered=0)
    store.rows = [
        {"event_type": mf.EVENT_STALE, "delivered": 0},
    ]

    report = mf.check_ml_model_health(store, alerts, model_path=model)

    event_types = [event for _sev, event, _msg, _key in alerts.sent]
    assert mf.EVENT_NOT_DELIVERED in event_types, "未投递必须升级为独立告警"
    assert report["alerts"][0]["delivered"] is False


def test_guard_is_fail_safe():
    """调用方是交易主循环 —— 检测失败绝不能抛。"""

    class ExplodingAlerts:
        def send(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    class ExplodingStore:
        def alerts(self, limit=200):
            raise RuntimeError("boom")

    report = mf.check_ml_model_health(
        ExplodingStore(), ExplodingAlerts(), model_path=Path("/nonexistent/x.pkl")
    )
    assert isinstance(report, dict)
    assert "health" in report


def test_summary_line_mentions_scoring_mode():
    line = mf.freshness_summary_line(
        {"scoring_label": "⚠️ 规则降级", "age_days": 44.0, "age_source": "pkl.mtime",
         "freshness_label": "过旧"}
    )
    assert "规则降级" in line and "44 天" in line
