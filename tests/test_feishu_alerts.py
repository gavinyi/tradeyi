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


def test_alert_manager_dispatches_feishu_card(tmp_path):
    # 必须用 tmp_path：此前写死 `data/test_alert_feishu.db`（仓库内运行期目录），
    # 一旦有 Worker 或另一个 pytest 进程占用同名文件就 PermissionError/WinError 32，
    # 变成与代码无关的环境假红；而且它违反「测试不得写工作区运行期数据」。
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


def test_hard_stop_limit_invalid_is_whitelisted():
    """P1-1 之一：事件必须在推送白名单里。

    **不在白名单的后果**：`AlertManager.send` 对**新** ERROR 事件会**提前 return**
    （`alerts.py` 的 `if not opened and event_type not in WHITELIST: return`）——
    既不推 webhook，也不推飞书卡片。**用户完全看不到**，等于静默失效。

    这正是 P1-1 的原缺陷形态："库里有、用户看不到"。
    """
    assert "HARD_STOP_LIMIT_INVALID" in FEISHU_GROUP_ALERT_WHITELIST, (
        "HARD_STOP_LIMIT_INVALID 不在白名单 —— 新事件会被 AlertManager 提前 return，"
        "用户收不到，而它是'硬止损永不触发'的唯一线索"
    )


def test_entry_rejection_events_are_whitelisted_and_deliver(tmp_path):
    """P1-1 之五：**开仓拒绝**的两条事件必须进白名单，并真的推出去。

    为什么单独测这两条：`run_gamma_strategy.py:1395` / `:1413` **已经在调 `alerts.send`**，
    但事件不在白名单时，`send()` 对未 opened 的新 ERROR 事件会**提前 return** ——
    接线接好了也推不出去。这是「第二重静默」，与 `HARD_STOP_LIMIT_INVALID` 的原缺陷同构。

    后果比退出路径更重：开仓被拒意味着策略根本拿不到仓位；一旦 `record_incident` 置为
    open 且首次投递恰好失败，后续**每次**拒绝都会提前 return —— 永久静默地被拒绝开仓。
    这正是 10-22 item 36「检出但 delivered=0」的形态。
    """
    for event_type in ("VOLATILITY_STOP_LIMIT_MISSING", "VOLATILITY_STOP_LIMIT_INVALID"):
        assert event_type in FEISHU_GROUP_ALERT_WHITELIST, (
            f"{event_type} 不在白名单 —— 开仓被拒绝时会静默，用户永远不知道该策略没开仓"
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
            assert mock_send.call_count == 1, (
                f"{event_type} 卡片没被调用 —— 走了'提前 return'，用户收不到"
            )


def test_hard_stop_limit_invalid_actually_delivers(tmp_path):
    """P1-1 之二：**本地投递链路**验证（不是"真实飞书送达"）。

    ① 产生 alert 行（落库与推送解耦的前提）；
    ② 飞书卡片**被调用**（证明它真的走了推送分支）；
    ③ `delivered` 为真且留痕（证明推送**成功**，不是"发了但没到"）。

    缺任何一件，都退化成"以为推了，其实没推"。
    """
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
            "卡片没被调用 —— 说明它走了'提前 return'那条路，用户收不到"
        )
        assert rows[0]["delivered"], "投递必须成功并留痕（delivered 为真）"


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


def test_alert_not_in_whitelist_new_incident_is_silent(tmp_path):
    """反向对照：不在白名单的新 ERROR 事件**落库但 delivered=false** —— 白名单是投递的必要条件。

    实测纠正一个常见误解：`send` 的提前 return 只在 `record_incident` 判定
    **不是新开事件**（`opened=False`，即同一 incident 复报）时生效；**首次**事件仍会落库。
    因此历史形态不是"库里没有"，而是 **"库里有、delivered=false、用户看不到"**
    —— 这正是 10-22 item 36 的形态，也是 P1-1 必须进白名单的理由。
    """
    store = TradingStore(str(tmp_path / "non_whitelisted.db"))
    alerts = AlertManager(store=store)
    with patch(
        "futu_quant.feishu_reporter.FeishuReporter.send_alert_card", return_value=True
    ) as mock_send:
        alerts.send("ERROR", "SOME_NON_WHITELISTED_EVENT", "未入白名单演练")
        assert mock_send.call_count == 0, "未入白名单的事件不该推群"
        rows = store.alerts()
        assert len(rows) == 1, "首次事件仍落库"
        assert not rows[0]["delivered"], (
            "未入白名单却 delivered=true —— 会让人误以为已送达"
        )

        # 复报（opened=False）才走提前 return：连行都不再新增
        alerts.send("ERROR", "SOME_NON_WHITELISTED_EVENT", "未入白名单演练-复报")
        assert len(store.alerts()) == 1, "复报走了提前 return，不该新增行"


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

    **不对称的后果**：首次告警若正好投递失败，`record_incident` 已把该 incident 置为 open；
    之后每次拒绝都命中 `alerts.py` 的 `if not opened and event_type not in WHITELIST: return`
    —— 这条 campaign 会**永久静默地被拒绝开仓**（10-22 item 36「检出但 delivered=0」）。
    开仓拒绝比退出路径的回落更该让人看见。
    """
    missing = [e for e in OPEN_BLOCK_EVENTS if e not in FEISHU_GROUP_ALERT_WHITELIST]
    assert not missing, (
        f"{missing} 不在白名单 —— 首次投递失败后，后续每次开仓拒绝都会被静默吞掉"
    )


def test_open_entry_block_events_actually_deliver(tmp_path):
    """两条开仓拒绝事件真的走推送分支：卡片被调用 + `delivered=true` 留痕。"""
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
        "卡片调用次数不足 —— 有事件走了「提前 return」，用户收不到"
    )
    assert all(r["delivered"] for r in rows), "投递成功却没留 delivered=true 痕迹"


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

