"""
测试飞书群实时告警与故障恢复链路 (Feishu Real-time Alert Test Suite)
严格隔离：单元测试必须 Mock 真实网络请求，禁止污染生产飞书群。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from futu_quant.alerts import FEISHU_GROUP_ALERT_WHITELIST, AlertManager
from futu_quant.store import TradingStore

ROOT = Path(__file__).resolve().parents[1]


def test_alert_manager_dispatches_feishu_card(tmp_path, monkeypatch):
    # 必须用 tmp_path：此前写死 `data/test_alert_feishu.db`（仓库内运行期目录），
    # 一旦有 Worker 或另一个 pytest 进程占用同名文件就 PermissionError/WinError 32，
    # 变成与代码无关的环境假红；而且它违反「测试不得写工作区运行期数据」。
    #
    # P0-2 起必须显式声明「通道已配置」：`_push_feishu` 用 `feishu_configured()` 区分
    # **未尝试(-1)** 与 **失败(0)**，通道没配时**不会**调用 reporter。
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/hook")
    db_path = tmp_path / "test_alert_feishu.db"
    store = TradingStore(str(db_path))
    alerts = AlertManager(store=store)

    with patch("futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=True) as mock_send:
        # 1. 触发一次 CRITICAL 告警
        alerts.send("CRITICAL", "OPEND_DISCONNECTED", "OpenD 网关掉线演练测试", incident_key="TEST_INCIDENT")
        alert_rows = store.alerts()
        assert len(alert_rows) == 1
        assert alert_rows[0]["severity"] == "CRITICAL"
        assert alert_rows[0]["event_type"] == "OPEND_DISCONNECTED"
        assert mock_send.call_count == 1
        assert alert_rows[0]["feishu_delivered"] == 1, "投递成功必须留下三态留痕"

        # 2. 验证防刷屏：立即再次发送相同告警，不重复记录，也不重复推送
        alerts.send("CRITICAL", "OPEND_DISCONNECTED", "OpenD 网关掉线演练测试", incident_key="TEST_INCIDENT")
        assert len(store.alerts()) == 1
        assert mock_send.call_count == 1

        # 3. 触发故障恢复
        recovered = alerts.recover("OPEND_DISCONNECTED", "OpenD 连接恢复正常", incident_key="TEST_INCIDENT")
        assert recovered is True

        # 验证产生了 RECOVERED 记录 (id 倒序排列，第一条为最新)
        alert_rows2 = store.alerts()
        assert len(alert_rows2) == 2
        assert alert_rows2[0]["event_type"] == "OPEND_DISCONNECTED_RECOVERED"
        assert mock_send.call_count == 2


# --------------------------------------------------------------------------
# P1-1：HARD_STOP_LIMIT_INVALID 必须"推出去"，不只是落库
# --------------------------------------------------------------------------


def test_task_timeout_warning_is_whitelisted():
    """WARNING 级任务超时必须进入强制投递白名单，禁止只落库不通知。"""
    assert "TASK_TIMEOUT" in FEISHU_GROUP_ALERT_WHITELIST


def test_hard_stop_limit_invalid_is_whitelisted():
    """P1-1 之一：事件必须在推送白名单里。

    白名单在 P0-2 之后仍有**两处**作用：
    ① `recover()` **只对白名单事件**发恢复卡片 —— 不进白名单就只报故障、不报恢复；
    ② 它是「这些事件必须被人看见」的显式清单，与 `ALERT_NEVER_MUTE` 相互印证。

    旧缺陷形态（"不在白名单的 ERROR 会被提前 return，连库都不落"）**已在 P0-2 删除**：
    现在 ERROR 及以上**默认推送**。这条断言保留，是为了不让白名单被清空 ——
    清空会让上面的恢复卡片链路一起断掉。
    """
    assert "HARD_STOP_LIMIT_INVALID" in FEISHU_GROUP_ALERT_WHITELIST, (
        "HARD_STOP_LIMIT_INVALID 不在白名单 —— 恢复卡片不会再发，"
        "而它是'硬止损永不触发'的唯一线索"
    )


def test_entry_rejection_events_are_whitelisted_and_deliver(tmp_path, monkeypatch):
    """P1-1 之五：**开仓拒绝**的两条事件必须进白名单，并真的推出去。

    为什么单独测这两条：`run_gamma_strategy.py:1395` / `:1413` **已经在调 `alerts.send`**，
    接线接好了还要确认它真的走到推送分支。

    白名单在 P0-2 之后的作用：① `recover()` 只对白名单事件发**恢复卡片**；
    ② 这两条同时在 `ALERT_NEVER_MUTE` 里 —— **静音配置覆盖不了它们**，
    否则「被拒绝开仓」可以被人为静默，而它是策略根本没拿到仓位的唯一线索。
    （旧那句「不在白名单会提前 return、永久静默」的机制**已在 P0-2 删除**。）
    """
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/hook")
    for event_type in ("VOLATILITY_STOP_LIMIT_MISSING", "VOLATILITY_STOP_LIMIT_INVALID"):
        assert event_type in FEISHU_GROUP_ALERT_WHITELIST, (
            f"{event_type} 不在白名单 —— 恢复卡片不会再发，用户只知道被拒、不知道已恢复"
        )

        store = TradingStore(str(tmp_path / f"{event_type}.db"))
        alerts = AlertManager(store=store)
        with patch(
            "futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=True
        ) as mock_send:
            alerts.send(
                "ERROR", event_type, f"演练：{event_type}", incident_key=f"TEST_{event_type}",
            )
            rows = store.alerts()
            assert len(rows) == 1, f"{event_type} 必须产生 alert 行"
            assert rows[0]["delivered"], f"{event_type} 必须投递成功并留痕（delivered 为真）"
            assert rows[0]["feishu_delivered"] == 1, (
                f"{event_type} 三态没留痕 —— 投递与留痕必须一起验，不能只看'调用过'"
            )
            assert mock_send.call_count == 1, (
                f"{event_type} 卡片没被调用 —— 事件没走到推送分支，用户收不到"
            )


def test_hard_stop_limit_invalid_actually_delivers(tmp_path, monkeypatch):
    """P1-1 之二：**本地投递链路**验证（不是"真实飞书送达"）。

    ① 产生 alert 行（落库与推送解耦的前提）；
    ② 飞书卡片**被调用**（证明它真的走了推送分支）；
    ③ `delivered` 为真且三态 `feishu_delivered=1` 留痕（证明推送**成功**，不是"发了但没到"）。

    缺任何一件，都退化成"以为推了，其实没推"。
    """
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/hook")
    store = TradingStore(str(tmp_path / "hard_stop_alerts.db"))
    alerts = AlertManager(store=store)

    with patch(
        "futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=True
    ) as mock_send:
        alerts.send(
            "ERROR", "HARD_STOP_LIMIT_INVALID",
            "US.META261030P660000: 硬止损限额非法演练（ratio=0 / usd=0）",
            incident_key="TEST_HARD_STOP_LIMIT_INVALID",
        )

        rows = store.alerts()
        assert len(rows) == 1, "ERROR 事件必须产生 alert 行"
        assert rows[0]["event_type"] == "HARD_STOP_LIMIT_INVALID"
        assert rows[0]["severity"] == "ERROR"
        assert mock_send.call_count == 1, (
            "卡片没被调用 —— 事件没走到推送分支，用户收不到"
        )
        assert rows[0]["delivered"], "投递必须成功并留痕（delivered 为真）"
        assert rows[0]["feishu_delivered"] == 1, "三态必须留痕（feishu_delivered=1）"


def test_hard_stop_limit_invalid_delivery_failure_is_not_silent_but_flagged(tmp_path):
    """P1-1 之四：**投递失败必须落 `delivered=false`，不能假装送达**。

    只测"mock 返回 True"只证明调用意图；真实故障形态是 reporter 抛异常或返回 False。
    两种都必须：① 告警行仍然落库（不丢事件）；② `delivered` 为假（不伪造成功）。
    """
    store = TradingStore(str(tmp_path / "hard_stop_alerts_fail.db"))
    alerts = AlertManager(store=store)

    with patch(
        "futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=False
    ):
        alerts.send(
            "ERROR", "HARD_STOP_LIMIT_INVALID",
            "投递失败演练（reporter 返回 False）", incident_key="TEST_HS_FAIL_FALSE",
        )
    rows = store.alerts()
    assert len(rows) == 1, "投递失败也必须留库"
    assert rows[0]["delivered"] is False or not rows[0]["delivered"], (
        "reporter 返回 False 却记 delivered=true —— 伪造成功，等于静默"
    )

    with patch(
        "futu_quant.feishu_reporter.FeishuReporter.send_alert_card",
        side_effect=RuntimeError("飞书不可达"),
    ):
        alerts.send(
            "ERROR", "HARD_STOP_LIMIT_INVALID",
            "投递失败演练（reporter 抛异常）", incident_key="TEST_HS_FAIL_EXC",
        )
    rows = store.alerts()
    assert any(r["event_type"] == "HARD_STOP_LIMIT_INVALID" for r in rows)
    assert all(not r["delivered"] for r in rows), (
        "reporter 抛异常却记 delivered=true —— 比不推更危险（以为推了）"
    )


def test_non_whitelisted_error_pushes_and_reports_stay_logged(tmp_path, monkeypatch):
    """新契约（P0-2）：不在白名单的 ERROR **默认推**；复报**仍必须写行**。

    旧契约的两条都已作废，而且**旧契约本身就是缺陷**：
    ① 「未入白名单不该推群」—— 正是本次要修的断链（ERROR 过去默认不推群，
       今日 13 条 ERROR 全部 `delivered=0`）；
    ② 「复报走提前 return、连行都不新增」—— 该分支已删除，它让事件连线索都不留，
       事后无法审计「到底有没有报过」。

    这条是**契约反转的守卫**：把任一条旧行为改回来，它立刻转红。
    """
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/hook")
    store = TradingStore(str(tmp_path / "non_whitelisted.db"))
    alerts = AlertManager(store=store)
    with patch(
        "futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=True
    ) as mock_send:
        alerts.send("ERROR", "SOME_NON_WHITELISTED_EVENT", "未入白名单演练")
        rows = store.alerts()
        assert len(rows) == 1, "ERROR 必须落库"
        assert mock_send.call_count == 1, (
            "不在白名单的 ERROR 也必须默认推（P0-2 新契约）"
        )
        assert rows[0]["feishu_delivered"] == 1, "三态必须留痕"

        # 复报（opened=False）：事故已 OPEN，但**仍必须新增一行**
        alerts.send("ERROR", "SOME_NON_WHITELISTED_EVENT", "未入白名单演练-复报")
        assert len(store.alerts()) == 2, (
            "复报没有新增行 —— 提前 return 被改回来了（它让事件连线索都不留）"
        )


def test_hard_stop_limit_invalid_is_wired_to_alerts_send():
    """P1-1 之三：源码级 —— `_hard_stop_limits` 必须真的调 `alerts.send`。

    只断言"事件在白名单"是不够的：白名单只保证**如果**调用了就能推出去。
    原缺陷恰恰是**根本没调用**（只写 `operational_event`）。所以这条钉住调用本身。
    """
    src = (ROOT / "scripts" / "run_gamma_strategy.py").read_text(encoding="utf-8")
    start = src.index("def _hard_stop_limits")
    body = src[start:]
    nxt = body.find("\ndef ", 1)
    body = body[:nxt] if nxt != -1 else body

    assert "alerts.send(" in body, (
        "_hard_stop_limits 里没有 alerts.send —— HARD_STOP_LIMIT_INVALID 只落 "
        "operational_event，用户看不到（P1-1 原缺陷）"
    )
    assert '"HARD_STOP_LIMIT_INVALID"' in body, "事件名不一致，白名单与推送会对不上"


# --------------------------------------------------------------------------
# P1-1 对称性：开仓被限额门禁拒绝的两条事件同样必须"推出去"
# --------------------------------------------------------------------------
OPEN_BLOCK_EVENTS = ("VOLATILITY_STOP_LIMIT_MISSING", "VOLATILITY_STOP_LIMIT_INVALID")


def test_open_entry_block_events_are_whitelisted():
    """开仓拒绝事件必须在白名单里（与 HARD_STOP_LIMIT_INVALID 同构）。

    保留理由：① `recover()` 只对白名单事件发**恢复卡片**；
    ② 这两条同时在 `ALERT_NEVER_MUTE` 里 —— **静音配置覆盖不了**，
    否则「被拒绝开仓」可以被人为静默，而它是策略根本没拿到仓位的唯一线索。

    P0-2 之后 ERROR 默认推送，旧那句「首次投递失败后每次都被静默吞掉」已不成立
    （那个提前 return 分支已删除）。
    """
    missing = [e for e in OPEN_BLOCK_EVENTS if e not in FEISHU_GROUP_ALERT_WHITELIST]
    assert not missing, (
        f"{missing} 不在白名单 —— 恢复卡片不会再发，用户只知道被拒、不知道已恢复"
    )


def test_open_entry_block_events_actually_deliver(tmp_path, monkeypatch):
    """两条开仓拒绝事件真的走推送分支：卡片被调用 + `delivered=true` + 三态留痕。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/hook")
    store = TradingStore(str(tmp_path / "open_block_alerts.db"))
    alerts = AlertManager(store=store)

    with patch(
        "futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=True
    ) as mock_send:
        for event in OPEN_BLOCK_EVENTS:
            alerts.send(
                "ERROR", event, f"演练：{event} 已拒绝开仓",
                incident_key=f"TEST_{event}",
            )

    rows = store.alerts()
    assert {r["event_type"] for r in rows} == set(OPEN_BLOCK_EVENTS), (
        f"两条事件都必须落库，实际 {[r['event_type'] for r in rows]}"
    )
    assert mock_send.call_count == len(OPEN_BLOCK_EVENTS), (
        "卡片调用次数不足 —— 有事件没走到推送分支，用户收不到"
    )
    assert all(r["delivered"] for r in rows), "投递成功却没留 delivered=true 痕迹"
    assert all(r["feishu_delivered"] == 1 for r in rows), "三态没留痕（feishu_delivered=1）"


def test_open_entry_block_events_are_wired_to_alerts_send():
    """源码级：`run_campaign` 内两条拒绝分支都必须调 `executor.alerts.send(...)`。

    只加白名单不改调用点等于没修；只改调用点不加白名单会被 `send` 吞掉。
    两侧都断言，缺一侧这条链路就断。

    **用 AST 按事件名精确配对，不用计数** —— 第一版写成
    `body.count("executor.alerts.send(") >= 2`，实测**变异删掉一条调用仍然绿**
    （`run_campaign` 里另有 3 处 `alerts.send`，删一条还剩 2 条，计数法被掩盖）。
    计数是"有几处调用"，不是"这个事件被推出去"。
    """
    import ast

    src = (ROOT / "scripts" / "run_gamma_strategy.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    run_campaign = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run_campaign"
    )

    def _is_alerts_send(call: ast.Call) -> bool:
        func = call.func
        return (
            isinstance(func, ast.Attribute) and func.attr == "send"
            and isinstance(func.value, ast.Attribute) and func.value.attr == "alerts"
        )

    send_calls = [n for n in ast.walk(run_campaign) if isinstance(n, ast.Call) and _is_alerts_send(n)]
    assert send_calls, "run_campaign 内没有任何 alerts.send 调用"

    for event in OPEN_BLOCK_EVENTS:
        matching = [
            call for call in send_calls
            if any(
                isinstance(arg, ast.Constant) and arg.value == event
                for arg in [*call.args, *[k.value for k in call.keywords]]
            )
        ]
        assert matching, (
            f"{event} 没有任何一处 alerts.send 调用把事件名当参数传进去 —— "
            "这条开仓拒绝只会落 operational_event，用户看不到"
        )

