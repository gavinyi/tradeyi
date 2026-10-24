"""P1-9「暂停新开仓」：单一生效口径 + 只写租户键。

守三件事：
1. 生效值：租户键优先、全局键兜底（改键名不能把已停的实盘悄悄放开）；
2. 写侧：只写租户键；
3. 拿不到租户时**拒绝写**，绝不回落全局 —— 写全局会把其他租户一起停掉，
   那正是 10-23 修掉的漏洞，页面又可能把它重新引回来。

变异：让 `set_volatility_new_entries_paused` 在空租户时写全局键 → 第 3 条转红。
"""

from __future__ import annotations

import pytest

from futu_quant.store import TradingStore
from futu_quant.volatility_pause import (
    LEGACY_KEY,
    set_volatility_new_entries_paused,
    volatility_new_entries_paused_for_tenant,
)

REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"


def test_effective_value_prefers_tenant_then_global(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    store.set_setting(LEGACY_KEY, "false")
    store.set_setting(f"{LEGACY_KEY}:{REAL}", "true")

    assert volatility_new_entries_paused_for_tenant(store, REAL) is True
    assert volatility_new_entries_paused_for_tenant(store, PAPER) is False
    # 空租户只能看全局（监控页面在没有租户上下文时的诚实读数）
    assert volatility_new_entries_paused_for_tenant(store, "") is False


def test_global_fallback_keeps_legacy_pause_effective(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    store.set_setting(LEGACY_KEY, "true")
    assert volatility_new_entries_paused_for_tenant(store, REAL) is True


def test_setter_writes_tenant_key_only(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    key = set_volatility_new_entries_paused(store, REAL, True)

    assert key == f"{LEGACY_KEY}:{REAL}"
    assert store.get_setting(f"{LEGACY_KEY}:{REAL}") == "true"
    # 绝不顺手写全局：写全局会把别的租户一起停掉
    assert store.get_setting(LEGACY_KEY) == ""
    # 其他租户不受影响
    assert store.get_setting(f"{LEGACY_KEY}:{PAPER}") == ""


def test_setter_refuses_without_tenant(tmp_path):
    store = TradingStore(tmp_path / "t.db")
    with pytest.raises(ValueError):
        set_volatility_new_entries_paused(store, "", True)
    assert store.get_setting(LEGACY_KEY) == "", "拿不到租户时绝不能回落写全局"
