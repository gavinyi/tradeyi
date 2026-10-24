"""P2-1a 接线守卫：ML 健康告警必须真的发出去、且投递状态可见。

为什么需要这条：P1-1 查出的形态是「库里有告警、用户看不到」——只写库不推送。
`ml_freshness.check_ml_model_health` 声称会回读 `delivered` 校验投递，这里把它钉住：
- 过期模型必须产出 `ML_MODEL_STALE` 告警行（不是只写 operational_events）；
- 报告里必须带 `delivered` 字段（True/False 皆可，**不能是 None 或缺键**），
  否则「告警有没有送达」这件事本身不可观测；
- 新鲜度阈值必须与 PRD 同源（30/35），页面与告警共用一份常量。
"""

from __future__ import annotations

import pickle
from datetime import datetime, timezone

import pytest

from futu_quant import ml_freshness as mf
from futu_quant.alerts import AlertManager
from futu_quant.ml_freshness import check_ml_model_health
from futu_quant.store import TradingStore


def _write_stale_model(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        pickle.dump(
            {
                "model": object(),
                "feature_cols": ["a"],
                "trained_at": "2020-01-01T00:00:00+00:00",
            },
            handle,
        )
    return path


def test_stale_model_emits_delivered_visible_alert(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    model = _write_stale_model(tmp_path / "ml_models" / "volatility_scorer_v3.pkl")
    alerts = AlertManager(store)

    report = check_ml_model_health(store, alerts, tenant_id="futu_real_master", model_path=model)

    stale_entries = [e for e in report["alerts"] if e["event_type"] == mf.EVENT_STALE]
    assert stale_entries, f"过期模型必须产出一条 ML_MODEL_STALE 告警，实际 {report['alerts']}"
    entry = stale_entries[0]
    assert "delivered" in entry, "告警条目必须带 delivered —— 否则无法判断有没有真的送达"
    assert entry["delivered"] is not None, "delivered 不能是 None（会被误读成已投递）"

    rows = [row for row in store.alerts(limit=20) if row.get("event_type") == mf.EVENT_STALE]
    assert rows, "告警必须落库（页面/投递中心才能看到）"


def test_freshness_thresholds_match_prd():
    """阈值必须与 PRD §三/§四 同源：<30 绿 / 30–34 橙 / ≥35 红。"""
    assert mf.DEFAULT_WARN_DAYS == 30
    assert mf.DEFAULT_STALE_DAYS == 35
    assert mf.classify_freshness(29.9) == mf.FRESHNESS_OK
    assert mf.classify_freshness(30) == mf.FRESHNESS_WARN
    assert mf.classify_freshness(35) == mf.FRESHNESS_STALE


def test_healthy_recent_model_emits_no_stale_alert(tmp_path):
    model = tmp_path / "ml_models" / "volatility_scorer_v3.pkl"
    model.parent.mkdir(parents=True, exist_ok=True)
    with open(model, "wb") as handle:
        pickle.dump(
            {
                "model": object(),
                "feature_cols": ["a"],
                "trained_at": datetime.now(timezone.utc).isoformat(),
            },
            handle,
        )
    store = TradingStore(tmp_path / "t.db")

    report = check_ml_model_health(store, AlertManager(store), tenant_id="futu_real_master", model_path=model)

    assert not [e for e in report["alerts"] if e["event_type"] == mf.EVENT_STALE], (
        "新鲜模型不得误报过期告警"
    )
