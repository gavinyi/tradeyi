"""候选退避 / 拒绝 / 通知去重键的**租户分段**守卫（Reviewer 19:2x 阻断项）。

失效形态（改前）：这三个键都不带租户段 ——
```
volatility_candidate_retry_until:{symbol}
volatility_candidate_retry_z:{symbol}
volatility_campaign_rejection:{symbol}
volatility_scanner_notified:{symbol}
```
雷达扇出后，先扫的租户（模拟）把候选退避写进共享键，后扫的租户（实盘）
在 ``retry_seconds``（默认 1200 秒）内**静默少发现一次**，而且没有任何告警 ——
和 #1/#2 要根治的跨租户压制是同一类问题。

口径：断言**行为**（另一个租户还能不能建 campaign），不是断言键名字面量；
``test_same_tenant_still_cooled_down`` 反向钉住"没有把冷却关掉"。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from futu_quant import volatility_scanner as scanner  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402

REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"
SYMBOL = "US.NVDA"
#: roll_campaign 的约定：**返回的 code 仍等于该标的 = 没滚出新合约（阻断）**；
#: 返回其它合约代码 = 成功滚出。测试夹具必须按这个口径造。
BLOCKED = ({"code": SYMBOL, "_roll_diagnostics": {"top_candidates": [{"code": "US.X"}]}},
           "无合格候选合约")
CREATED = ({"code": "US.NVDA260101C100000"}, "ok")


class _AlertStub:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def send(self, *_args, **_kwargs) -> None:  # noqa: D401 - 测试桩
        pass


def _item() -> SimpleNamespace:
    return SimpleNamespace(
        regime={"active": True, "reason": "测试信号", "zscore_20d": 3.0, "checks": []},
        profile={"entry_zscore": 2.0, "minimum_underlying_move": 0.02},
    )


def _run(monkeypatch, store, tenant: str, rolled_result) -> tuple:
    monkeypatch.setattr(scanner, "AlertManager", _AlertStub)
    monkeypatch.setattr(scanner, "load_volatility_campaigns", lambda tenant_id: [])
    monkeypatch.setattr(scanner, "resolve_volatility_radar_budget", lambda symbol: 1000.0)
    monkeypatch.setattr(scanner, "roll_campaign", lambda broker, seed, store: rolled_result)
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        scanner, "upsert_campaign",
        lambda config_key, campaign, store=None: written.append((config_key, campaign)),
    )
    outcome = scanner.TenantScanResult(tenant_id=tenant)
    broker = SimpleNamespace(tenant_id=tenant, account_id="1", trading_env="SIMULATE")
    scanner._maybe_create_tenant_campaign(
        store, broker, tenant, f"VOLATILITY_CAMPAIGNS:{tenant}",
        SYMBOL, _item(), {}, outcome,
    )
    return outcome, written


def test_candidate_state_keys_are_tenant_scoped(tmp_path, monkeypatch):
    """阻断候选写出的冷却/拒绝/通知状态，必须落在**本租户**命名空间。"""
    store = TradingStore(tmp_path / "scoped.db")
    _run(monkeypatch, store, REAL, BLOCKED)

    assert store.get_setting(
        scanner._tenant_scoped_setting_key("volatility_candidate_retry_until", REAL, SYMBOL)
    ), "实盘的候选冷却没写进自己的租户段 —— 会压住模拟盘"
    assert not store.get_setting(
        scanner._tenant_scoped_setting_key("volatility_candidate_retry_until", PAPER, SYMBOL), ""
    ), "实盘写冷却时污染了模拟租户的键"
    assert store.get_setting(
        scanner._tenant_scoped_setting_key("volatility_scanner_notified", REAL, SYMBOL)
    ), "信号通知去重没有租户段 —— 先扫的租户会吞掉另一租户的告警"
    rows = store.decisions(limit=10)
    assert [row["tenant_id"] for row in rows] == [REAL], (
        f"CANDIDATE_BLOCKED 留痕缺租户：{[row['tenant_id'] for row in rows]}"
    )


def test_other_tenant_not_suppressed_by_cooldown(tmp_path, monkeypatch):
    """**行为断言**：实盘被冷却后，模拟盘对同一标的仍必须能建出 campaign。"""
    store = TradingStore(tmp_path / "isolation.db")
    _run(monkeypatch, store, REAL, BLOCKED)          # 实盘：候选被阻断，写 20 分钟冷却
    outcome, written = _run(monkeypatch, store, PAPER, CREATED)   # 模拟：同一标的同一轮

    assert outcome.campaigns_created == 1, (
        "模拟租户被实盘的候选冷却挡掉了 —— 跨租户静默少发现"
    )
    assert written and written[0][0] == f"VOLATILITY_CAMPAIGNS:{PAPER}"


def test_same_tenant_still_cooled_down(tmp_path, monkeypatch):
    """反向：租户**自己的**冷却必须照旧生效 —— 不能靠"把冷却关掉"来通过上一条。"""
    store = TradingStore(tmp_path / "self.db")
    _run(monkeypatch, store, REAL, BLOCKED)
    outcome, written = _run(monkeypatch, store, REAL, CREATED)

    assert outcome.campaigns_created == 0, "本租户的冷却失效了 —— 会变成每轮重复撞候选"
    assert written == []


def test_enable_key_is_tenant_scoped_never_global(tmp_path, monkeypatch):
    """控制面：建单只能启用**本租户**，不得写全局 `strategy_enabled`。

    全局写等于把策略对所有租户置为启用 —— 而且用户手动关掉全局后，
    雷达下一次建单会把它**静默重新启用**（正是今晚踩过的"静默改控制面"）。
    """
    store = TradingStore(tmp_path / "enable.db")
    store.set_setting("strategy_enabled:Volatility Long Gamma", "false")

    outcome, _ = _run(monkeypatch, store, REAL, CREATED)

    assert outcome.campaigns_created == 1
    assert store.get_setting("strategy_enabled:Volatility Long Gamma") == "false", (
        "雷达建单把全局启用键改成了 true —— 跨租户连坐 + 静默重新启用"
    )
    assert store.get_setting(
        f"strategy_enabled:{REAL}:Volatility Long Gamma"
    ) == "true", "本租户的启用声明没写进去 —— 调度器按这个键判定"
    assert store.get_setting(f"strategy_enabled:{REAL}:volatility_long_gamma") == "true"
    assert store.get_setting(f"strategy_enabled:{PAPER}:Volatility Long Gamma", "") == ""
