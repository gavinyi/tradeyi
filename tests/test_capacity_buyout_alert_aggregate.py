"""P1-8 守卫：买不起 WARNING **同因同租户每小时最多一条**（Ops 2026-11-05）。

背景（真实数据）：热修后 `VOLATILITY_CAPACITY_BUYOUT_FREQUENT` 在
`futu_real_master` 上 5 小时推了 **119 条**（≈24 条/小时），把信号埋了。

为什么既有 15 分钟去重挡不住：`AlertManager.send` 的去重键含 **message 全文**，
而这条消息里带着每轮递增的轮次数（6、7、8…），所以每轮都是"新消息"。
→ 去重必须在**产生端**按小时桶做，不能指望 message 去重。

聚合不是静音：推送的那一条仍带 24h 轮次数与最近一轮时间；
逐轮时间戳照旧落在 `volatility_capacity_buyout_rounds_24h:{tenant}` 窗口里。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from futu_quant import alerts as alerts_mod  # noqa: E402
from futu_quant.capacity_precheck import (  # noqa: E402
    BUYOUT_WARN_EVENT,
    BUYOUT_WARN_HOUR_KEY,
    BUYOUT_WARN_THRESHOLD,
    REASON_PREMIUM,
    CapacityPrecheck,
    _buyout_warn_bucket,
    _warn_frequent_buyouts,
)
from futu_quant.store import TradingStore  # noqa: E402

TENANT = "futu_real_master"
OTHER_TENANT = "futu_paper_sandbox"
#: 固定时刻，避免测试跨小时边界抖动（北京时间 2026-11-05 10:05 = 02:05Z）。
BASE = datetime(2026, 11, 5, 2, 5, tzinfo=timezone.utc)


def _bought_out() -> CapacityPrecheck:
    return CapacityPrecheck(
        equity=6640.73, line=398.44, reasons={REASON_PREMIUM: 2}, cheapest_premium=1740.0,
    )


@pytest.fixture
def sent(monkeypatch) -> list:
    """把真正的推送换掉：只记录，不碰网络/飞书。"""
    calls: list = []

    def fake_send(self, severity, event_type, message, incident_key=None, tenant_id=None):
        calls.append({
            "severity": severity,
            "event_type": event_type,
            "message": message,
            "incident_key": incident_key,
            "tenant_id": tenant_id,
        })

    monkeypatch.setattr(alerts_mod.AlertManager, "send", fake_send)
    return calls


def _fire(store, tenant: str, count: int, moment: datetime) -> bool:
    return _warn_frequent_buyouts(store, tenant, count, _bought_out(), moment)


# --------------------------------------------------------------------------- #
# 核心：同小时聚合
# --------------------------------------------------------------------------- #


def test_same_hour_many_rounds_emit_only_one_alert(tmp_path, sent):
    """同一小时 8 轮（轮次数 5→12 递增）只推 1 条 —— 这是 119 条刷屏的直接解药。"""
    store = TradingStore(tmp_path / "agg.db")
    pushed = [
        _fire(store, TENANT, count, BASE + timedelta(minutes=index * 5))
        for index, count in enumerate(range(BUYOUT_WARN_THRESHOLD, BUYOUT_WARN_THRESHOLD + 8))
    ]
    assert pushed == [True] + [False] * 7, "同小时只允许第一条推送，其余只累加计数"
    assert len(sent) == 1
    assert sent[0]["event_type"] == BUYOUT_WARN_EVENT
    assert sent[0]["severity"] == "WARNING"


def test_next_hour_emits_again(tmp_path, sent):
    """跨小时要再推一条，否则聚合就变成永久静音。"""
    store = TradingStore(tmp_path / "agg.db")
    assert _fire(store, TENANT, 6, BASE) is True
    assert _fire(store, TENANT, 7, BASE + timedelta(minutes=30)) is False
    assert _fire(store, TENANT, 8, BASE + timedelta(hours=1)) is True
    assert len(sent) == 2


def test_alert_carries_round_count_and_last_round_time(tmp_path, sent):
    """聚合后仍可追溯：消息里必须有 24h 轮次数与最近一轮时间。"""
    store = TradingStore(tmp_path / "agg.db")
    _fire(store, TENANT, 119, BASE)
    message = sent[0]["message"]
    assert "119" in message, "轮次数必须出现在聚合后的那一条里"
    assert "最近一轮" in message, "必须给出最近一轮时间，否则聚合=信息丢失"
    # 北京时间 10:05（UTC 02:05）
    assert "10:05" in message
    assert "每小时只聚合推送一条" in message


def test_below_threshold_never_alerts(tmp_path, sent):
    store = TradingStore(tmp_path / "agg.db")
    assert _fire(store, TENANT, BUYOUT_WARN_THRESHOLD - 1, BASE) is False
    assert sent == []


def test_tenants_do_not_share_bucket(tmp_path, sent):
    """租户隔离：A 已推过不能让 B 被吞掉。"""
    store = TradingStore(tmp_path / "agg.db")
    assert _fire(store, TENANT, 9, BASE) is True
    assert _fire(store, OTHER_TENANT, 9, BASE) is True
    assert len(sent) == 2
    assert {call["message"].split("]")[0] for call in sent} == {
        f"[{TENANT}", f"[{OTHER_TENANT}",
    }


def test_hour_bucket_uses_china_time(tmp_path, sent):
    """小时桶按北京时间切：UTC 16:30 与 UTC 17:30 属**不同**北京小时（次日 00:30 / 01:30）。"""
    store = TradingStore(tmp_path / "agg.db")
    moment = datetime(2026, 11, 4, 16, 30, tzinfo=timezone.utc)
    bucket_a, label_a = _buyout_warn_bucket(moment)
    bucket_b, label_b = _buyout_warn_bucket(moment + timedelta(hours=1))
    assert bucket_a != bucket_b
    assert label_a == "11-05 00:30" and label_b == "11-05 01:30"


def test_read_setting_failure_still_alerts(tmp_path, sent):
    """读小时桶失败必须**照常推送**（宁可多一条，也不静默）。"""
    store = TradingStore(tmp_path / "agg.db")
    key = BUYOUT_WARN_HOUR_KEY.format(tenant_id=TENANT)
    original = store.get_setting

    def flaky(target, default=""):
        if target == key:
            raise RuntimeError("模拟读库失败")
        return original(target, default)

    store.get_setting = flaky  # type: ignore[method-assign]
    assert _fire(store, TENANT, 9, BASE) is True
    assert len(sent) == 1


def test_alert_failure_does_not_raise(tmp_path, monkeypatch):
    """推送抛异常不得打断交易链路（沿用原语义）。"""
    store = TradingStore(tmp_path / "agg.db")

    def boom(self, *args, **kwargs):
        raise RuntimeError("推送炸了")

    monkeypatch.setattr(alerts_mod.AlertManager, "send", boom)
    assert _fire(store, TENANT, 9, BASE) is False
