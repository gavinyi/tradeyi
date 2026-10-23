"""状态键**写读一致**守卫 —— 一条顶三条。

要钉住的三件事（Chief 2026-10-23 在 `campaign_state` 实测：91 条里
8 条无租户形态 + 9 条带租户形态，两种格式**同时存在**）：

1. `order_push` 成交后写的**带租户**键（`volatility_long_gamma:{tenant}:{code}`），
   `run_gamma_strategy` **必须能读到** —— 否则策略侧看到的 `peak` /
   `entry_iv_actual` / `position_opened_at` 是旧的或不存在的，**止盈止损基准错**；
2. 生产存量的 **8 条无租户旧键**，读侧**必须兼容**（读到后收敛成新键，不丢状态）
   —— 丢了 `peak` 就是丢了止盈基准；
3. 写侧只产生新格式，旧键随下一轮自然收敛，**不留双份**。

**判据是行为，不是源码字符串**：真的跑 `run_campaign`，往传入的 `state` 里放一个
**哨兵字段**，跑完看它还在不在 —— 守卫读到哪个键，哨兵就跟着哪个键走。
断言源码字符串的守卫在这里会空转（注释里也有同样的字样）。

harness 复用 `test_hard_stop_end_to_end.py`（同一个 `run_campaign` 端到端跑法）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from futu_quant.store import TradingStore  # noqa: E402
from futu_quant.tenant import tenant_of_broker  # noqa: E402
from test_hard_stop_end_to_end import (  # noqa: E402
    CODE,
    _campaign,
    _load_gamma,
    _LossBroker,
    _RecordingExecutor,
)

STRATEGY = "volatility_long_gamma"
TENANT = "futu_real_master"
TENANT_KEY = f"{STRATEGY}:{TENANT}:{CODE}"
PLAIN_KEY = f"{STRATEGY}:{CODE}"
SENTINEL = "sentinel_fill_qty"


def _run(tmp_path, state: dict):
    """跑一次 VOLATILITY campaign，返回 (state, executor)。"""
    rg = _load_gamma()
    store = TradingStore(tmp_path / "state_read.db")
    executor = _RecordingExecutor(store)

    class _Calendar:
        @staticmethod
        def session(_now):
            return type("S", (), {"regular_open": staticmethod(lambda _n: True)})()

    rg.MARKET_CALENDAR.session = _Calendar.session
    rg.volatility_signal_state = lambda *a, **k: {}
    rg.sync_volatility_position_cycle = lambda *a, **k: None

    rg.run_campaign(_LossBroker(bid=7.4), executor, _campaign(), state, strategy_id=STRATEGY)
    return state, executor


def _assert_broker_resolves_to_the_paper_or_real_tenant():
    """前置：harness 的 broker 必须能解析出租户，否则守卫会静默走"无租户"分支。"""
    broker = _LossBroker(bid=7.4)
    resolved, _ = tenant_of_broker(broker, strict=False)
    assert resolved == TENANT, (
        f"harness broker 解析出的租户是 {resolved!r}，不是 {TENANT!r} —— "
        "守卫会静默走无租户分支，测的是假路径"
    )


def test_tenant_scoped_state_written_by_order_push_is_readable(tmp_path):
    """`order_push` 写的带租户键，`run_gamma_strategy` **必须读到**。

    做法：把 `order_push` 会写的形态（`{strategy}:{tenant}:{code}`）放进 state，
    跑完 `run_campaign` 后断言**这个 dict 被当作 `campaign_state` 用了** ——
    判据用 `active_contract_code`（`run_campaign` 只会往 `campaign_state` 里写它），
    而不是"哨兵还在不在"：只验"键没被删"是**空转**的（变异 M1 已证：
    读侧完全不看带租户键时，哨兵照样原样留着，守卫仍绿）。
    """
    _assert_broker_resolves_to_the_paper_or_real_tenant()

    state, _executor = _run(
        tmp_path, {TENANT_KEY: {"peak": 0.0, SENTINEL: 7.0}},
    )
    assert TENANT_KEY in state, "带租户的状态键整个消失了"
    assert state[TENANT_KEY].get(SENTINEL) == 7.0, (
        f"带租户键里的哨兵字段丢了 —— 读侧把整个键换掉了（实际 state：{state}）"
    )
    assert state[TENANT_KEY].get("active_contract_code") == CODE, (
        "带租户键**没有被当作 campaign_state 使用** —— 读侧没读到 `order_push` 写的"
        f"状态键，peak / entry_iv_actual / position_opened_at 全部取不到"
        f"（实际 state：{state}）"
    )


def test_legacy_tenantless_state_is_still_readable_and_migrates(tmp_path):
    """生产存量的**无租户旧键**必须仍可读，且读到后**收敛成新键**（不丢、不留双份）。

    这条是迁移期的生命线：8 条存量状态键里存着 `peak` 等止盈止损基准，
    读侧不兼容就等于当场把基准丢掉。
    """
    _assert_broker_resolves_to_the_paper_or_real_tenant()

    state, _executor = _run(
        tmp_path, {PLAIN_KEY: {"peak": 0.0, SENTINEL: 9.0}},
    )
    assert state.get(TENANT_KEY, {}).get(SENTINEL) == 9.0, (
        f"无租户旧键的哨兵没有收敛到带租户键（实际 state：{state}）—— "
        "存量状态被丢弃或留在了旧键上"
    )
    assert state.get(TENANT_KEY, {}).get("active_contract_code") == CODE, (
        "收敛过来的旧状态**没有被当作 campaign_state 使用** —— "
        f"读侧读了别的键（实际 state：{state}）"
    )
    assert PLAIN_KEY not in state, (
        f"旧键没有收敛掉，新旧两份并存（实际 state：{state}）—— "
        "写侧必须是读侧的超集，双份会让 worker 每轮当两个 campaign 执行"
    )
