"""日亏急停线必须按**账户生效配置**取，不能只读 .env。

缺陷形态（2026-10-30 排查确认）：

`scripts/run_service.py` 的权益快照任务里，账户急停判定原先写的是

    limit = RiskLimits.load().max_daily_loss

`RiskLimits.load()` **只读 .env**（`MAX_DAILY_LOSS`）。而页面 27 上配的租户级
`risk_limits:{tenant}` / 账户级 `risk` 段走的是 `resolve_risk_limits` —— 那是
下单风控的唯一入口。于是同一个账户有**两条互相不知道的日亏线**：

| 路径 | 取线方式 | 实盘实际值 |
| :--- | :--- | :--- |
| 下单风控 `RiskEngine` | `resolve_risk_limits` | **$400**（租户配置） |
| 日亏巡检（账户急停唯一触发点） | `RiskLimits.load()` | $10,000（.env） |

结果是：实盘配的 $400 在这条巡检里**从未生效**，要亏到 $10,000 才急停 —— 25 倍宽松。
而且这条巡检是**唯一**会写 `kill_switch` 的地方，所以放宽就等同于没有这条线。

三条守卫，缺一不可：

1. **原语**：`resolve_risk_limits` 对两个不同租户确实返回**不同的**日亏线
   （证明"按账户解析"这件事本身成立，不是永远返回同一个数）；
2. **接线**：`run_service.resolve_daily_loss_limit` 真的调它，且**不再**直接用
   `RiskLimits.load()` 取线（源码守卫，防有人改回去）；
3. **退回路径**：配置残缺时退回 .env 值**必须留痕 + 推群**，因为退回意味着
   阈值可能被放宽 —— 静默退回就是"风控自己降级而没人知道"。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from futu_quant.alerts import FEISHU_GROUP_ALERT_WHITELIST
from futu_quant.risk import RiskLimits
from futu_quant.risk_limits import (
    clear_risk_limits_cache,
    resolve_risk_limits,
    risk_limits_tenant_key,
)
from futu_quant.store import TradingStore

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import run_service  # noqa: E402

REAL_ACCOUNT = "281756481476169109"
REAL_TENANT = "futu_real_master"
PAPER_ACCOUNT = "20170229"
PAPER_TENANT = "futu_paper_sandbox"

#: 生产页面 27 的真实配置（实盘 $400 / 模拟盘 $100,000）—— 用它当测试值，
#: 守卫测的就是生产那组数，不是虚构造出来的数字。
REAL_DAILY_LOSS = 400.0
PAPER_DAILY_LOSS = 100_000.0
ENV_DAILY_LOSS = 10_000.0

#: `ACCOUNT_RISK_FIELDS` 要求「部分配置即拒绝」，只写一项必然触发 ConfigError。
PARTIAL_RISK = {"max_daily_loss": REAL_DAILY_LOSS}


class _Broker:
    """最小 broker 替身：`resolve_risk_limits` 只读 account_id / trading_env。"""

    def __init__(self, account_id: str, trading_env: str):
        self.account_id = account_id
        self.trading_env = trading_env


class _AlertsRecorder:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def send(self, severity: str, event_type: str, message: str, **kwargs):
        self.calls.append((severity, event_type, message))
        return True


def _seed(store: TradingStore, tenant: str, max_daily_loss: float) -> None:
    """只写**租户键**（与生产配置形态一致，不靠全局键掩盖解析路径）。"""
    store.set_setting(
        risk_limits_tenant_key(tenant),
        json.dumps({"max_daily_loss": max_daily_loss}, ensure_ascii=False),
    )


def _seed_full(store: TradingStore, tenant: str, max_daily_loss: float) -> None:
    """写一份**完整**的 risk 段（所有字段齐全，才不会被 fail-closed 拒绝）。"""
    from futu_quant.risk_limits import ACCOUNT_RISK_FIELDS

    section = {}
    for name, kind in ACCOUNT_RISK_FIELDS:
        if name == "max_daily_loss":
            section[name] = max_daily_loss
        else:
            section[name] = 1_000_000 if kind is int else 0.5
    store.set_setting(risk_limits_tenant_key(tenant), json.dumps(section))


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_risk_limits_cache()
    yield
    clear_risk_limits_cache()


# --------------------------------------------------------------------------- #
# 1. 原语：两个租户解析出两条线
# --------------------------------------------------------------------------- #

def test_resolve_returns_per_tenant_daily_loss(tmp_path):
    """同一 store 里两个租户各配一条线 → 各自解析出**自己那条**。

    这条先红的话，说明"按账户取线"这个前提不成立，后面两条守卫都没意义。
    """
    store = TradingStore(tmp_path / "per_tenant.db")
    _seed_full(store, REAL_TENANT, REAL_DAILY_LOSS)
    _seed_full(store, PAPER_TENANT, PAPER_DAILY_LOSS)

    real = resolve_risk_limits(store, _Broker(REAL_ACCOUNT, "REAL"))
    paper = resolve_risk_limits(store, _Broker(PAPER_ACCOUNT, "SIMULATE"))

    assert real.max_daily_loss == REAL_DAILY_LOSS, (
        f"实盘解析出的日亏线是 {real.max_daily_loss}，不是配置里的 {REAL_DAILY_LOSS}"
    )
    assert paper.max_daily_loss == PAPER_DAILY_LOSS, (
        f"模拟盘解析出的日亏线是 {paper.max_daily_loss}，不是配置里的 {PAPER_DAILY_LOSS}"
    )
    assert real.max_daily_loss != paper.max_daily_loss, (
        "两个租户解析出同一条线 —— 解析入口没有按账户区分，等于全局一条线"
    )


def test_env_value_differs_from_real_tenant_value(tmp_path):
    """前置守卫：.env 的默认线与实盘租户线**确实**不同，缺陷才有危害。

    如果两者恰好相等，那么"只读 .env"就是无害的，整篇注释都在描述一个不存在的问题。
    """
    env_line = RiskLimits.load().max_daily_loss
    assert env_line != REAL_DAILY_LOSS, (
        f".env 的日亏线（{env_line}）与实盘配置（{REAL_DAILY_LOSS}）相同 —— "
        "该缺陷此时无实际危害，本文件的守卫失去意义，需重新评估"
    )


# --------------------------------------------------------------------------- #
# 2. 接线：巡检真的走解析入口
# --------------------------------------------------------------------------- #

def test_daily_loss_limit_helper_uses_configured_line(tmp_path):
    """`resolve_daily_loss_limit` 必须返回**配置值**、来源 `resolved`。"""
    store = TradingStore(tmp_path / "helper_ok.db")
    _seed_full(store, REAL_TENANT, REAL_DAILY_LOSS)
    alerts = _AlertsRecorder()

    limit, source = run_service.resolve_daily_loss_limit(
        store, _Broker(REAL_ACCOUNT, "REAL"), alerts, account_id=REAL_ACCOUNT,
    )

    assert limit == REAL_DAILY_LOSS, (
        f"日亏线取成了 {limit}（来源 {source}）—— 巡检仍在读 .env，"
        f"配置的 {REAL_DAILY_LOSS} 不生效"
    )
    assert source == "resolved", f"配置完整却标成 {source} —— 退回路径被误触发"
    assert not alerts.calls, f"配置正常却发了告警：{alerts.calls}"


def test_equity_snapshot_task_does_not_load_env_limit_directly():
    """源码守卫：巡检主流程不得再用 `RiskLimits.load()` 取线。

    行为测试只能证明 helper 是对的；这一条证明**主流程接的正是那个 helper**。
    两者分开写，是因为"helper 正确但没人调用"是一种真实的失效形态。

    注意**只允许一处** `RiskLimits.load()`：helper 内部的退回路径。退回路径是
    显式放弃配置值、且已留痕告警，语义正确；巡检主流程里再出现一次就说明
    "取线"这件事又分叉了两份实现。
    """
    src = (ROOT / "scripts" / "run_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    helper = next(
        (node for node in ast.walk(tree)
         if isinstance(node, ast.FunctionDef) and node.name == "resolve_daily_loss_limit"),
        None,
    )
    assert helper is not None, (
        "run_service.py 里没有 resolve_daily_loss_limit —— 巡检没接上解析入口"
    )

    def _env_loads(node) -> int:
        return sum(
            1 for sub in ast.walk(node)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "load"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "RiskLimits"
        )

    inside = _env_loads(helper)
    total = _env_loads(tree)
    assert inside == 0, (
        f"helper 内不允许 RiskLimits.load()：失败时不得静默回落 .env，实际 {inside} 次"
    )
    assert total == 0, (
        f"RiskLimits.load() 全文件出现 {total} 次 —— 日亏线不能从 .env 猜测或静默回落"
    )

    # 急停判定必须使用 helper 返回的 limit，而不是另算一个
    kill_block = src.split("DAILY_LOSS_KILL_SWITCH", 1)[0]
    assert "resolve_daily_loss_limit(" in kill_block, (
        "急停判定之前没有调用解析入口 —— 接在了别的位置，判定用的仍是旧线"
    )


# --------------------------------------------------------------------------- #
# 3. 退回路径：配置残缺 → 退回 .env，但**必须**留痕 + 推群
# --------------------------------------------------------------------------- #

def test_partial_config_falls_back_to_env_with_visible_trace(tmp_path):
    """租户配置残缺且无成功历史时不得使用 .env，必须返回不可用并告警。"""
    store = TradingStore(tmp_path / "helper_fallback.db")
    _seed(store, REAL_TENANT, REAL_DAILY_LOSS)      # 只有一项 → ConfigError
    alerts = _AlertsRecorder()

    limit, source = run_service.resolve_daily_loss_limit(
        store, _Broker(REAL_ACCOUNT, "REAL"), alerts, account_id=REAL_ACCOUNT,
    )

    assert source == "unavailable", f"无历史成功值却返回 {source}，不得使用 .env 值"
    assert limit is None and limit != ENV_DAILY_LOSS

    events = [
        row for row in store.operational_events(50)
        if row["event_type"] == "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK"
    ]
    assert events, "退回 .env 却没有运行事件 —— 阈值变宽这件事事后无从查起"
    assert events[0]["status"] == "ERROR", (
        f"无可用历史时状态是 {events[0]['status']}，应为 ERROR"
    )
    payload = json.loads(events[0]["payload_json"])
    assert payload.get("account_id") == REAL_ACCOUNT, f"事件未带账户：{payload}"
    assert payload.get("candidate_limit") is None, f"无历史时不应有候选阈值：{payload}"
    assert payload.get("age_seconds") is None, f"无历史时年龄应为空：{payload}"
    assert payload.get("source") == "unavailable", f"来源未标不可用：{payload}"

    assert alerts.calls, "日亏线不可用却没有发 ERROR 告警"
    severity, event_type, message = alerts.calls[0]
    assert event_type == "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK"
    assert severity == "ERROR", f"退回路径只发了 {severity} —— 风控降级应为 ERROR"
    assert REAL_ACCOUNT in message, f"告警未带账户：{message}"


def test_fallback_event_is_whitelisted_for_push():
    """退回事件必须进飞书白名单 —— 否则它只落库、不推送，等于静默。

    这正是本仓库反复出现的「库里有、delivered=0、用户看不到」形态。
    """
    assert "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK" in FEISHU_GROUP_ALERT_WHITELIST, (
        "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK 不在飞书白名单 —— "
        "退回 .env 的告警用户收不到，风控降级会静默发生"
    )


def test_zero_daily_loss_limit_is_preserved_and_not_env_fallback(tmp_path, monkeypatch):
    """零值是有效急停线，不能被当作缺失值或静默替换为 .env。"""
    store = TradingStore(tmp_path / "zero-limit.db")
    alerts = _AlertsRecorder()
    monkeypatch.setattr(
        run_service, "resolve_risk_limits",
        lambda _store, _broker: SimpleNamespace(max_daily_loss=0.0),
    )

    limit, source = run_service.resolve_daily_loss_limit(
        store, _Broker(PAPER_ACCOUNT, "SIMULATE"), alerts, account_id=PAPER_ACCOUNT,
    )

    assert limit == 0.0
    assert source == "resolved"
    assert not any(call[1] == "DAILY_LOSS_LIMIT_RESOLVE_FALLBACK" for call in alerts.calls)


def test_helper_resolves_without_network_when_alerts_recorded(tmp_path):
    """回归对照：配置完整时 helper **不得**触碰告警通道（避免每次巡检都推一条）。"""
    store = TradingStore(tmp_path / "no_noise.db")
    _seed_full(store, PAPER_TENANT, PAPER_DAILY_LOSS)
    alerts = _AlertsRecorder()

    with patch("futu_quant.feishu_reporter.FeishuReporter.send_alert_card") as sender:
        run_service.resolve_daily_loss_limit(
            store, _Broker(PAPER_ACCOUNT, "SIMULATE"), alerts, account_id=PAPER_ACCOUNT,
        )

    assert not sender.called, "配置正常的一次巡检触发了飞书投递 —— 会产生告警疲劳"
