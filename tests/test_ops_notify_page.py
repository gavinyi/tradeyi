"""P0-UI-2 第二刀守卫：通知与运维页 = 静音清单**唯一可编辑入口**。

这一刀要满足 Reviewer 的四条判据（2026-11-01）：

1. **写入层必须等于读取层**：
   - 写：`update_mute_list()` → `mutate_runtime_config("alert_mute_list")` → `runtime_config` 表；
   - 读：`AlertManager._mute_list()` → 先 `get_runtime_config("alert_mute_list")`（同表），
     再回落 `get_setting`。**同表同层**，因此改完下一轮告警即按新清单判定。
   本文件用**真实读取链**验证：写完之后 `AlertManager._mute_list()` 必须看到新清单
   （只测「写入成功」抓不到「写进去但读不到」，这轮已经栽过两次）。

2. **旧写入口本批删掉**：`pages/40_alert_delivery_center.py` 曾是静音清单编辑器，
   现改为只读 + 跳转，不得再调用 `update_mute_list`。

3. **不可静音清单是代码常量**：写入侧必须拒绝，页面侧必须只读展示。

4. **密钥不进页面**：webhook / secret 不在页面输入框里编辑。
"""

from __future__ import annotations

import ast
from pathlib import Path

from futu_quant.alerts import (
    ALERT_MUTE_LIST_KEY,
    ALERT_NEVER_MUTE,
    AlertManager,
    update_mute_list,
)
from futu_quant.store import TradingStore

ROOT = Path(__file__).resolve().parents[1]
OPS_PAGE = ROOT / "pages" / "47_ops_and_notifications.py"
LEGACY_PAGE = ROOT / "pages" / "40_alert_delivery_center.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 写后必须能被真实读取链读到（判据 2 的模板写法）
# ---------------------------------------------------------------------------


def test_mute_list_write_is_visible_to_alert_manager(tmp_path):
    store = TradingStore(tmp_path / "mute.db")
    manager = AlertManager(store)

    accepted, rejected = update_mute_list(
        store, ["PORTFOLIO_STRESS_BREACH"], actor="ops", reason="unit-test",
    )

    assert accepted == {"PORTFOLIO_STRESS_BREACH"}
    assert rejected == []
    assert manager._mute_list() == {"PORTFOLIO_STRESS_BREACH"}, (
        "写完 AlertManager 读不到 —— 说明写入层与读取层不一致"
    )


def test_mute_list_clears_and_manager_sees_empty(tmp_path):
    store = TradingStore(tmp_path / "clear.db")
    manager = AlertManager(store)
    update_mute_list(store, ["TASK_TIMEOUT"], actor="ops", reason="seed")

    update_mute_list(store, [], actor="ops", reason="clear")

    assert manager._mute_list() == set()


def test_never_mutable_events_are_rejected_by_writer(tmp_path):
    store = TradingStore(tmp_path / "never.db")
    manager = AlertManager(store)
    never = sorted(ALERT_NEVER_MUTE)[0]

    accepted, rejected = update_mute_list(store, [never], actor="ops", reason="try")

    assert never in rejected, "不可静音事件必须被写入侧拒绝"
    assert never not in accepted
    assert never not in manager._mute_list()


def test_wildcard_entries_are_rejected(tmp_path):
    store = TradingStore(tmp_path / "wild.db")
    accepted, rejected = update_mute_list(store, ["RECONCILIATION_*", "*"], actor="ops")

    assert accepted == set()
    assert len(rejected) == 2


def test_write_goes_to_runtime_config_table(tmp_path):
    """写读同表的显式证据：runtime_config 里能读到刚写的清单。"""
    store = TradingStore(tmp_path / "table.db")
    update_mute_list(store, ["TASK_TIMEOUT"], actor="ops", reason="table-check")

    cfg = store.get_runtime_config(ALERT_MUTE_LIST_KEY)

    assert cfg, "runtime_config 里没有 alert_mute_list —— AlertManager 优先读的就是这张表"
    assert "TASK_TIMEOUT" in str(cfg[0])


# ---------------------------------------------------------------------------
# 2. 页面层：唯一可编辑入口 + 旧入口只读
# ---------------------------------------------------------------------------


def test_ops_page_is_the_editor():
    source = _source(OPS_PAGE)

    assert "update_mute_list(" in source, "本页必须是静音清单的唯一写入点"
    assert "render_admin_login()" in source, "改告警去留需要管理员登录"
    assert "批准人" in source, "静音是人工决策，必须能追溯到人"
    assert "ALERT_MUTE_LIST_KEY" in source, "读取用共享键常量，不手拼字符串"


def test_ops_page_shows_never_mutable_readonly():
    source = _source(OPS_PAGE)

    assert "ALERT_NEVER_MUTE" in source
    assert "is_never_mutable(" in source, "页面提交前也要挡一次不可静音项"


def test_ops_page_does_not_collect_secrets():
    """密钥不进页面输入框（webhook / secret 只在 .env 或密钥卡片维护）。"""
    source = _source(OPS_PAGE)

    for banned in ("FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK_SECRET"):
        assert f'"{banned}"' not in source
    assert "st.text_input(\"webhook" not in source.lower()


def test_legacy_page_no_longer_writes_mute_list():
    """判据 3：迁走就必须删旧入口，否则同一配置两处可改。"""
    source = _source(LEGACY_PAGE)

    assert "update_mute_list(" not in source, "旧页面仍在写静音清单 —— 出现第二个写入口"
    assert "47_ops_and_notifications.py" in source, "旧页面必须给跳转"


def test_only_one_page_writes_mute_list():
    """全站扫描：写静音清单的页面只能有 1 个。"""
    offenders = []
    for path in sorted((ROOT / "pages").glob("*.py")):
        if "update_mute_list(" in _source(path):
            offenders.append(path.name)

    assert offenders == ["47_ops_and_notifications.py"], offenders


def test_ops_page_imports_resolve():
    from futu_quant.operational_readiness import _audit_page_imports

    tree = ast.parse(_source(OPS_PAGE), filename=str(OPS_PAGE))
    assert _audit_page_imports(ROOT, tree, OPS_PAGE.name) == []
