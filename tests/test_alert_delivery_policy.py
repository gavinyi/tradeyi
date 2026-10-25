"""告警推送策略守卫（P0-2 S2）。

每条判据都对应一种「本次要修的断链」—— 判据必须**能失败**，否则是给自己发合格证：

* `test_open_incident_still_writes_row` —— 原 `alerts.py:67-68` 的提前 return：
  「事故已 OPEN 且不在白名单」时**连告警行都不写**。把那个分支加回来，本用例转红。
* `test_error_pushes_by_default` —— ERROR 级事件过去默认**不推群**（只有白名单/CRITICAL 才推）。
* `test_never_mute_beats_mute_list` —— 不可静音清单必须胜过静音配置。
* `test_mute_list_rejects_wildcards` —— 静音清单禁止通配（否则静默范围不可审计）。
* `test_muted_error_still_persisted` —— 静音仍落库（`muted=1`），只是不推。
* `test_existing_db_gains_tristate_columns` —— 三列必须走**幂等 ALTER**：
  只写进 `CREATE TABLE` 的话，存量库永远拿不到这三列 → 功能上线即失效。
"""

from __future__ import annotations

import sqlite3

import pytest

from futu_quant.alerts import (
    ALERT_MUTE_LIST_KEY,
    AlertManager,
    feishu_configured,
    is_never_mutable,
    parse_mute_list,
    update_mute_list,
)
from futu_quant.store import TradingStore


class FakeStore:
    """只实现 `AlertManager` 用到的那几个方法。"""

    def __init__(self, *, mute_list: str = "", opened: bool = True, feishu_ok: bool = True):
        self.rows: list[dict] = []
        self.mute_list = mute_list
        self.opened = opened
        self.feishu_ok = feishu_ok

    def record_incident(self, key, severity, message):
        return (self.opened, 1)

    def alert_recently_exists(self, severity, event_type, message, seconds=900):
        # 与生产同语义：同 (severity, event_type, message) 已写过即视为「近期存在」。
        # 桩必须真实现这个语义 —— 否则「去重」这类判据测的是桩的空实现。
        return any(
            row["severity"] == severity
            and row["event_type"] == event_type
            and row["message"] == message
            for row in self.rows
        )

    def alerts(self, limit=200):
        # 与生产同序：最新在前。
        return list(reversed(self.rows))[:limit]

    def get_setting(self, key, default=""):
        if key in ("FEISHU_WEBHOOK_URL", "FEISHU_GROUP_CHAT_ID"):
            return "https://example.invalid/hook" if self.feishu_ok else ""
        if key == ALERT_MUTE_LIST_KEY:
            return self.mute_list
        return default

    def add_alert(self, severity, event_type, message, delivered=False, **kwargs):
        # 与真实 `TradingStore.add_alert` 同口径：给了三态就由三态推导 `delivered`。
        # 桩必须和真身同语义 —— 否则测的是桩的行为，不是生产的行为。
        webhook_delivered = kwargs.get("webhook_delivered", -1)
        feishu_delivered = kwargs.get("feishu_delivered", -1)
        if webhook_delivered != -1 or feishu_delivered != -1:
            delivered = max(webhook_delivered, feishu_delivered) > 0
        self.rows.append({
            "severity": severity, "event_type": event_type, "message": message,
            "delivered": delivered, **kwargs,
        })
        return len(self.rows)


@pytest.fixture
def fake_feishu(monkeypatch):
    """拦掉飞书网络调用，记录投递尝试。"""
    calls: list[tuple] = []

    class FakeReporter:
        def __init__(self, store=None):
            self.store = store

        def send_alert_card(self, severity, event_type, message, is_recovered=False):
            calls.append((severity, event_type, is_recovered))
            return True

    monkeypatch.setattr("futu_quant.feishu_reporter.FeishuReporter", FakeReporter)
    return calls


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FEISHU_GROUP_CHAT_ID", raising=False)


def _manager(store: FakeStore) -> AlertManager:
    return AlertManager(store)


# --------------------------------------------------------------------------
# ① 删掉的提前 return —— 事故已 OPEN 也必须留下告警行
# --------------------------------------------------------------------------


def test_open_incident_still_writes_row(fake_feishu):
    """`record_incident` 返回「未新开」（事故已 OPEN）时，**仍必须写告警行**。

    原实现：`if not opened and event_type not in WHITELIST: return` ——
    事件连线索都不留，事后无法审计「到底有没有报过」。
    """
    store = FakeStore(opened=False)  # 事故已 OPEN，且事件不在白名单
    _manager(store).send("ERROR", "PORTFOLIO_STRESS_BREACH", "组合压力超限")

    assert store.rows, "事故已 OPEN 时告警行被丢掉了 —— 正是本次要修的断链"
    assert store.rows[0]["event_type"] == "PORTFOLIO_STRESS_BREACH"


# --------------------------------------------------------------------------
# ② ERROR 默认推
# --------------------------------------------------------------------------


def test_error_pushes_by_default(fake_feishu):
    """不在白名单的 ERROR 也必须推（过去默认不推群）。"""
    store = FakeStore()
    _manager(store).send("ERROR", "PORTFOLIO_STRESS_BREACH", "组合压力超限")

    row = store.rows[0]
    assert row["feishu_delivered"] == 1, "ERROR 默认必须推飞书"
    assert row["delivered"] is True
    assert row["muted"] == 0


def test_whitelisted_warning_still_pushed(fake_feishu):
    """白名单里的 WARNING（如纸面亏损告警线）不能被「只推 ERROR」收窄掉。"""
    store = FakeStore()
    _manager(store).send("WARNING", "PAPER_DAILY_LOSS_WARN", "纸面亏损触及告警线")

    assert store.rows[0]["feishu_delivered"] == 1, "白名单 WARNING 必须仍然推"


# --------------------------------------------------------------------------
# ③ 不可静音胜过静音清单
# --------------------------------------------------------------------------


def test_never_mute_beats_mute_list(fake_feishu):
    store = FakeStore(mute_list='["LEDGER_MISMATCH"]')
    _manager(store).send("ERROR", "LEDGER_MISMATCH", "三账不平")

    row = store.rows[0]
    assert row["muted"] == 0, "不可静音清单必须胜过静音配置"
    assert row["feishu_delivered"] == 1


def test_reconciliation_prefix_is_never_mutable():
    assert is_never_mutable("RECONCILIATION_STATE_DRIFT") is True
    assert is_never_mutable("LEDGER_MISMATCH") is True
    assert is_never_mutable("PORTFOLIO_STRESS_BREACH") is False


# --------------------------------------------------------------------------
# ④ 静音：不推，但仍落库
# --------------------------------------------------------------------------


def test_muted_error_still_persisted(fake_feishu):
    store = FakeStore(mute_list='["SOME_NEW_ERROR"]')
    _manager(store).send("ERROR", "SOME_NEW_ERROR", "已静音的事件")

    assert store.rows, "静音不等于消失 —— 必须仍然落库"
    row = store.rows[0]
    assert row["muted"] == 1
    assert row["feishu_delivered"] == -1, "静音 = 未尝试投递（-1），不是投递失败（0）"
    assert row["webhook_delivered"] == -1
    assert row["delivered"] is False


# --------------------------------------------------------------------------
# ⑤ 静音清单禁止通配
# --------------------------------------------------------------------------


def test_mute_list_rejects_wildcards():
    accepted, rejected = parse_mute_list('["RECONCILIATION_*", "OK_ONE"]')

    assert accepted == {"OK_ONE"}
    assert "RECONCILIATION_*" in rejected, "通配必须被拒绝 —— 否则静默范围不可审计"


@pytest.mark.parametrize("bad", ['["*"]', '["A*B"]', '["A B"]', '[""]', '["A?"]'])
def test_mute_list_rejects_bad_entries(bad):
    accepted, rejected = parse_mute_list(bad)
    assert accepted == set()
    assert rejected


def test_mute_list_tolerates_broken_json():
    accepted, rejected = parse_mute_list("{not json")
    assert accepted == set()
    assert rejected


def test_rejected_mute_entry_is_reported_once(fake_feishu):
    """非法静音项**只留一行痕**。

    `_mute_list()` 在推送判定里、**每次 `send()` 都会执行**，所以留痕若不去重，
    静音清单写错一次就会「每条告警写一行」`ALERT_MUTE_LIST_ENTRY_REJECTED` ——
    `alerts` 表无界增长，且这条告警自己还压不掉。
    """
    store = FakeStore(mute_list='["RECONCILIATION_*"]')
    manager = _manager(store)

    for index in range(5):
        manager.send("ERROR", f"SOME_ERROR_{index}", f"第 {index} 条")

    rejected_rows = [
        row for row in store.rows if row["event_type"] == "ALERT_MUTE_LIST_ENTRY_REJECTED"
    ]
    assert len(rejected_rows) == 1, (
        f"非法静音项写了 {len(rejected_rows)} 行 —— 去重失效，会无界增长"
    )


# --------------------------------------------------------------------------
# ⑥ 三列必须走幂等 ALTER（存量库也要拿到）
# --------------------------------------------------------------------------


def test_existing_db_gains_tristate_columns(tmp_path, monkeypatch):
    """**存量库**打开后必须自动补上三列 —— 而且必须走**存量库路径**。

    这是 S1 的核心判据。`initialize()` 对存量库在 `apply_migrations()` 之后就 `return`
    （`store.py:190-191`），所以把三列写在「新建库」分支里的实现，**存量库永远拿不到** ——
    2026-10-24 部署实测正是这个形态：生产 `alerts` 仍 8 列，而新 `add_alert` 已引用
    这三列 → **下一次写告警直接 OperationalError**。

    做法：先用新库路径建出完整 schema，再**删掉三列 + 抹掉迁移记录**，
    模拟「真实生产存量库缺这三列」，然后重新打开。

    旧版本这条测试只建了一张 `alerts` 表（**没有 `order_intents`**）→ 于是走了
    **新建库**分支 → 绿得没有意义。夹具必须包含 `order_intents`，否则测的不是存量路径。
    """
    db = tmp_path / "legacy.db"
    TradingStore(db)  # 建出完整 schema（新库路径）

    raw = sqlite3.connect(db)
    raw.execute(
        "INSERT INTO alerts(severity,event_type,message,delivered,created_at)"
        " VALUES('ERROR','LEGACY','存量行',0,'2026-01-01T00:00:00+00:00')"
    )
    for column in ("webhook_delivered", "feishu_delivered", "muted"):
        raw.execute(f"ALTER TABLE alerts DROP COLUMN {column}")
    raw.execute("DELETE FROM schema_migrations WHERE version IN (8,9,10)")
    raw.commit()
    before = {row[1] for row in raw.execute("PRAGMA table_info(alerts)")}
    assert not ({"webhook_delivered", "feishu_delivered", "muted"} & before), (
        "夹具失效：三列没被删掉，这条测试测不出「存量库补列」"
    )
    assert raw.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'"
    ).fetchone() is not None, "夹具失效：没有 order_intents，会走新建库分支"
    raw.close()

    # 存量库升级必须显式授权（与 run_service 启动路径同口径），未授权时 __init__ 会拒绝
    monkeypatch.setenv("TRADEYI_ALLOW_MIGRATION", "1")
    store = TradingStore(db)  # 必须走存量库路径
    with store.connection() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(alerts)")
        }
    assert {"webhook_delivered", "feishu_delivered", "muted"} <= columns, (
        "存量库没有拿到三列 —— 迁移被放在「新建库」分支里了（存量库走不到那里）"
    )

    # 存量行保持默认值，**不伪回填**
    legacy = [row for row in store.alerts(limit=10) if row["event_type"] == "LEGACY"][0]
    assert legacy["webhook_delivered"] == -1
    assert legacy["feishu_delivered"] == -1
    assert legacy["muted"] == 0
    assert legacy["delivered"] == 0


def test_tristate_columns_come_from_migrations_not_create_table(tmp_path):
    """三列必须由**迁移**补齐（新建库也一样）—— 判据用行为，不用源码形状。

    新库打开后 `schema_migrations` 里必须记录 8/9/10。若谁把列写回 `CREATE TABLE alerts`
    又去掉迁移，版本号就不会被记录 → 本用例转红。
    """
    store = TradingStore(tmp_path / "fresh.db")
    with store.connection() as connection:
        versions = {
            int(row["version"])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
    assert {8, 9, 10} <= versions, (
        "三列不是由迁移补齐的（schema_migrations 缺 8/9/10）—— 只有一条迁移路径"
    )


def test_delivered_is_derived_from_tristate(tmp_path):
    """`delivered` 由三态推导（`max(...) > 0`）—— 单一真值来源，不会自相矛盾。"""
    store = TradingStore(tmp_path / "t.db")

    store.add_alert("ERROR", "E_MUTED", "m", webhook_delivered=-1, feishu_delivered=-1, muted=1)
    store.add_alert("ERROR", "E_OK", "m", webhook_delivered=0, feishu_delivered=1)
    store.add_alert("ERROR", "E_FAIL", "m", webhook_delivered=0, feishu_delivered=0)

    rows = {row["event_type"]: row for row in store.alerts(limit=10)}
    assert rows["E_MUTED"]["delivered"] == 0
    assert rows["E_MUTED"]["muted"] == 1
    assert rows["E_OK"]["delivered"] == 1
    assert rows["E_FAIL"]["delivered"] == 0


# --------------------------------------------------------------------------
# ⑦ S3 投递探针：`!= 1` 即失败（-1 与 0 都要能报）
# --------------------------------------------------------------------------


def test_probe_events_are_never_mutable():
    """探针必须真实投递 —— 进不可静音清单，否则「静音」就等于「不投递」，什么也验不到。"""
    assert is_never_mutable("ALERT_DELIVERY_PROBE") is True
    assert is_never_mutable("ALERT_CHANNEL_NOT_CONFIGURED") is True
    assert is_never_mutable("ALERT_DELIVERY_FAILED") is True


def test_probe_reports_unconfigured_channel(fake_feishu):
    """通道未配置（-1）**必须**上报。

    只认 `0` 的话，**通道空着时探针永远不告警** —— 正好漏掉本次要修的核心场景。
    """
    store = FakeStore(feishu_ok=False)
    result = _manager(store).probe_delivery()

    assert result["feishu"] == -1
    assert "ALERT_CHANNEL_NOT_CONFIGURED" in result["escalations"]


def test_probe_reports_delivery_failure(fake_feishu, monkeypatch):
    """通道配置了但投递失败（0）→ `ALERT_DELIVERY_FAILED`。"""
    store = FakeStore()
    manager = _manager(store)
    monkeypatch.setattr(manager, "_push_feishu", lambda *a, **k: 0)

    result = manager.probe_delivery()

    assert result["feishu"] == 0
    assert "ALERT_DELIVERY_FAILED" in result["escalations"]


def test_probe_does_not_flag_a_working_channel(fake_feishu):
    store = FakeStore()
    result = _manager(store).probe_delivery()

    assert result["feishu"] == 1, "配置好的飞书通道必须投递成功"
    assert "ALERT_DELIVERY_FAILED" not in result["escalations"], "成功通道不得报投递失败"


def test_channel_status_reports_per_channel():
    status = _manager(FakeStore(feishu_ok=False)).channel_status()
    assert status == {"webhook": False, "feishu": False}


def test_feishu_configured_uses_only_real_keys():
    class Store:
        def __init__(self, settings):
            self.settings = settings

        def get_setting(self, key, default=""):
            return self.settings.get(key, default)

    assert feishu_configured(Store({"FEISHU_WEBHOOK_URL": "https://x"})) is True
    assert feishu_configured(Store({"FEISHU_GROUP_CHAT_ID": "oc_x"})) is True
    assert feishu_configured(Store({})) is False


# --------------------------------------------------------------------------
# ⑧ S4：静音清单写入（留痕 + 禁通配）与「未投递」视图口径
# --------------------------------------------------------------------------


class _ConfigStore:
    """只记录配置写入的桩。"""

    def __init__(self):
        self.written = None
        self.actor = None

    def mutate_runtime_config(self, key, default, updater, actor="system"):
        self.written = updater(default)
        self.actor = actor
        return self.written, 1

    def audit(self, *_args, **_kwargs):
        return None


def test_update_mute_list_rejects_wildcards_and_does_not_write():
    store = _ConfigStore()
    accepted, rejected = update_mute_list(store, ["RECONCILIATION_*"], "alice", "试试")

    assert accepted == set()
    assert rejected
    assert store.written is None, "非法项不得写库 —— 否则「配置写了但没生效」"


def test_update_mute_list_writes_with_actor():
    store = _ConfigStore()
    accepted, rejected = update_mute_list(store, ["PORTFOLIO_STRESS_BREACH"], "alice", "降噪")

    assert rejected == []
    assert accepted == {"PORTFOLIO_STRESS_BREACH"}
    assert store.written == ["PORTFOLIO_STRESS_BREACH"]
    assert store.actor == "alice", "必须留下批准人（actor）"


def test_undelivered_view_excludes_muted_and_non_error(tmp_path):
    """「未投递」视图口径：`ERROR/CRITICAL AND delivered=0 AND muted=0`。

    静音是人工决策、不是故障 —— 混进来就会把这个视图训练成噪声，
    而它是本次修复的核心可见性。
    """
    store = TradingStore(tmp_path / "views.db")
    store.add_alert("ERROR", "E_UNDELIVERED", "m", webhook_delivered=-1, feishu_delivered=-1)
    store.add_alert("ERROR", "E_MUTED", "m", webhook_delivered=-1, feishu_delivered=-1, muted=1)
    store.add_alert("WARNING", "W_UNDELIVERED", "m", webhook_delivered=-1, feishu_delivered=-1)
    store.add_alert("ERROR", "E_DELIVERED", "m", webhook_delivered=-1, feishu_delivered=1)

    undelivered = {row["event_type"] for row in store.undelivered_alerts()}
    assert undelivered == {"E_UNDELIVERED"}, f"未投递视图口径错：{undelivered}"

    muted = {row["event_type"] for row in store.muted_alerts()}
    assert muted == {"E_MUTED"}, "被静音的必须仍然可见（静音不是抹除）"
