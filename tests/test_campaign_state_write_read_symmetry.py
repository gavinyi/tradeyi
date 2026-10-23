"""P0-9 守卫：campaign state 的写读必须对称 —— 写进去的键，一个完整周期后必须还在。

背景（Architect 实测，当前树上唯一会**静默删状态**的地方）：
  `order_push.register_fill_to_campaign` 写 `volatility_long_gamma:{tenant_id}:{symbol}`；
  而 `run_volatility_strategy.run_once` 的 `active_keys` 只认不带租户的
  `volatility_long_gamma:{code}`，于是同一轮里：
    - `:221` 的过滤把带租户的键剔出 state；
    - `:223` 的 `prune_campaign_states` 再把它当 stale 删掉。
  后果：P0-1 的「成交即登记」（`registered_fill_*`）写进去、同一个周期就被删 ——
  **P0-1 的全部意义被这一处抵消**。

守卫口径（Chief 指定）：**端到端** —— 断言 `order_push` 写进去的状态键，
经过一个完整 `run_once` 后**仍然存在**；不是断言某个变量、也不是断言键名存在。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from futu_quant.config import upsert_campaign  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402

TENANT_ID = "futu_real_master"
SYMBOL = "US.META261030P660000"
UNDERLYING = "US.META"
TENANT_KEY = f"volatility_long_gamma:{TENANT_ID}:{SYMBOL}"
PLAIN_KEY = f"volatility_long_gamma:{SYMBOL}"


class _FlatBroker:
    account_id = "281756481476169109"
    trading_env = "REAL"

    def positions(self) -> pd.DataFrame:
        return pd.DataFrame()

    def orders(self) -> pd.DataFrame:
        return pd.DataFrame()

    def is_cancellable_status(self, status) -> bool:
        return str(status).upper() in {"SUBMITTED", "WAITING_SUBMIT", "SUBMITTING"}


def _settings_stub():
    """`run_once` 入口门禁的 Settings 桩：只放行 paper 下单开关。"""

    class _S:
        enable_paper_orders = True

        @classmethod
        def load(cls, *args, **kwargs):
            return cls

    return _S


def _seed(store: TradingStore) -> dict:
    """造出 order_push 会写的那两把键 + 一条 campaign。"""
    store.save_campaign_states({
        TENANT_KEY: {
            "fill_registered_at": "2026-10-23T00:00:00+00:00",
            "registered_fill_qty": 1.0,
            "registered_fill_price": 10.0,
        },
        PLAIN_KEY: {"fill_registered_at": "2026-10-23T00:00:00+00:00"},
    })
    campaign = {
        "code": SYMBOL, "underlying": UNDERLYING, "enabled": True, "exit_only": True,
        "legs": [{"code": SYMBOL, "side": "BUY", "ratio": 1, "role": "PRIMARY"}],
    }
    # ⚠️ **必须传 `store=`**：`upsert_campaign` 的 `store=None` 分支是
    # `store = store or _TradingStore()` → **直连生产库**。
    # 10-23 事故：此处不传 store，生产 `VOLATILITY_CAMPAIGNS` 被整表清空
    # （8 条真实 campaign → 1 条测试夹具）。conftest 已加全局兜底，
    # 但调用点仍应显式传 —— 显式优于依赖兜底。
    upsert_campaign("VOLATILITY_CAMPAIGNS", campaign, store)
    return campaign


def test_tenant_scoped_state_key_survives_full_cycle(tmp_path, monkeypatch):
    """端到端：order_push 写进去的带租户状态键，跑完一个完整 run_once 后必须还在。"""
    import run_volatility_strategy as rv

    store = TradingStore(tmp_path / "state.db")
    campaign = _seed(store)

    # run_once 从全局读取状态与 campaign —— 重定向到临时库（测试严禁触生产库）
    monkeypatch.setattr(rv, "load_state", lambda: store.campaign_states())
    monkeypatch.setattr(rv, "load_volatility_campaigns", lambda *_a, **_k: [campaign])
    # 入口门禁读全局配置 —— 用桩放行（本用例只关心状态剪除，不关心下单开关）
    monkeypatch.setattr(rv, "Settings", _settings_stub())

    class _Executor:
        def __init__(self, store):
            self.store = store

    broker = _FlatBroker()
    executor = _Executor(store)

    before = store.campaign_states()
    assert TENANT_KEY in before, "前置条件不成立：带租户的状态键未写入"

    rv.run_once(reconcile=False, broker=broker, executor=executor)

    after = store.campaign_states()
    assert TENANT_KEY in after, (
        f"P0-9 复现：带租户的状态键在一个完整周期后被静默删除。\n"
        f"  写侧 order_push：{TENANT_KEY}\n"
        f"  周期后剩余键：{sorted(after)}"
    )
    # 登记内容也不得丢失（P0-1 的可见效果）
    assert after[TENANT_KEY].get("fill_registered_at"), "成交登记时间丢失"
    assert float(after[TENANT_KEY].get("registered_fill_qty") or 0) == 1.0, "登记数量丢失"


def test_unowned_state_key_is_still_pruned(tmp_path, monkeypatch):
    """回归：不属于任何在管 campaign 的状态键仍应被剪掉（护栏不是"永不剪"）。"""
    import run_volatility_strategy as rv

    store = TradingStore(tmp_path / "state2.db")
    campaign = _seed(store)
    orphan_key = f"volatility_long_gamma:{TENANT_ID}:US.GHOST260101C100000"
    store.save_campaign_states({orphan_key: {"stale": True}})

    monkeypatch.setattr(rv, "load_state", lambda: store.campaign_states())
    monkeypatch.setattr(rv, "load_volatility_campaigns", lambda *_a, **_k: [campaign])
    monkeypatch.setattr(rv, "Settings", _settings_stub())

    class _Executor:
        def __init__(self, store):
            self.store = store

    rv.run_once(
        reconcile=False, broker=_FlatBroker(), executor=_Executor(store),
    )

    after = store.campaign_states()
    assert TENANT_KEY in after, "在管 campaign 的带租户键被误删"
    assert orphan_key not in after, "无归属的 stale 键未被剪除（剪除功能失效）"
