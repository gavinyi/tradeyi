"""跨租户状态剪枝守卫（2026-10-23 开盘实测缺陷）。

失效形态（改前）：`run_volatility_once` 是逐租户执行的，但 `campaign_state` 是共享表，
旧实现按「本租户 active_keys + 本租户 active_codes」判定 stale 并**全局前缀删除** ——
另一租户的带租户状态键（其 code 不在本租户 active_codes 里）被当成 stale 删掉；
对方下一轮用 `setdefault(..., {"peak": 0.0})` 重建 → **实盘利润保护状态每两分钟归零**。

口径：断言**行为**（另一租户的键与值必须还在），不是断言函数返回了什么字面量。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src", ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from futu_quant.store import TradingStore  # noqa: E402
from run_volatility_strategy import prunable_state_keys  # noqa: E402

REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"
REAL_CODE = "US.AMD261030P520000"
PAPER_CODE = "US.QQQ261029C735000"
LEGACY_CODE = "US.NVDA261106P215000"

REAL_KEY = f"volatility_long_gamma:{REAL}:{REAL_CODE}"
PAPER_KEY = f"volatility_long_gamma:{PAPER}:{PAPER_CODE}"
LEGACY_KEY = f"volatility_long_gamma:{LEGACY_CODE}"


def _store(tmp_path) -> TradingStore:
    store = TradingStore(tmp_path / "prune.db")
    store.save_campaign_states({
        REAL_KEY: {"peak": 0.42, "signal_epoch": 7},
        PAPER_KEY: {"peak": 0.10, "signal_epoch": 1},
        LEGACY_KEY: {"peak": 0.05},
    })
    return store


def test_other_tenant_keys_never_pruned(tmp_path):
    """模拟租户那轮剪枝，绝对不能删掉实盘的带租户键与值。"""
    store = _store(tmp_path)

    stale = prunable_state_keys(store, PAPER, active_keys=set(), active_codes={PAPER_CODE})
    assert REAL_KEY not in stale, "实盘的状态键被模拟租户判为 stale —— 跨租户删除"
    store.delete_campaign_states(stale)

    states = store.campaign_states()
    assert REAL_KEY in states, "实盘状态键被模拟那轮删掉了 —— peak 会被重置"
    assert states[REAL_KEY]["peak"] == 0.42, "实盘状态值被改写了"
    assert PAPER_KEY in states, "本租户自己的活跃键被删了"


def test_own_stale_legacy_key_still_pruned(tmp_path):
    """反向：本租户与历史无租户形态的 stale 键必须照旧被清 —— 不能靠"什么都不删"通过。"""
    store = _store(tmp_path)

    stale = prunable_state_keys(store, PAPER, active_keys=set(), active_codes={PAPER_CODE})
    assert LEGACY_KEY in stale, "历史无租户的 stale 键没被清理（会永远堆积）"
    assert PAPER_KEY not in stale, "本租户活跃 code 对应的键不该被删"


def test_real_tenant_prune_does_not_touch_paper(tmp_path):
    """对称方向：实盘那轮也不能删模拟租户的键。"""
    store = _store(tmp_path)

    stale = prunable_state_keys(store, REAL, active_keys=set(), active_codes={REAL_CODE})
    assert PAPER_KEY not in stale
    store.delete_campaign_states(stale)
    assert PAPER_KEY in store.campaign_states()
