"""P1-6 执行模式单源 + 降级收口守卫。

守三件事：
1. 解析阶梯唯一：租户策略键 → 旧无前缀策略键 → 全局 `execution_mode` → 空；
2. 降级必须写**租户键**（+ 旧键兜底）：只写旧键会被租户键盖过 → 降级失效；
3. 人工恢复是**删键**（回到原配置），不是写 PAPER —— 实盘账户上 PAPER 会被
   白名单自动提升成 REAL，写 PAPER 会制造「以为在 PAPER、其实在 REAL」。

变异：把 `downgrade_strategy_execution_mode` 里的租户键写入删掉（只写旧键）→
第 2 条转红；把 `restore_strategy_execution_mode` 改成写 PAPER → 第 3 条转红。
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from futu_quant import execution_mode as mode_mod
from futu_quant.execution import ExecutionManager
from futu_quant.execution_mode import (
    downgrade_strategy_execution_mode,
    resolve_configured_execution_mode,
    restore_strategy_execution_mode,
    strategy_execution_mode_override,
)
from futu_quant.store import TradingStore

REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"


def _fake_tenants():
    return [
        SimpleNamespace(tenant_id=REAL, is_active=True),
        SimpleNamespace(tenant_id=PAPER, is_active=True),
    ]


def test_resolve_configured_mode_ladder(tmp_path):
    store = TradingStore(tmp_path / "t.db")

    # 什么都没有 → 空串（调用方自己决定默认值）
    assert resolve_configured_execution_mode(store, REAL, "core_satellite") == ""

    # 全局模式
    store.set_setting("execution_mode", "PAPER")
    assert resolve_configured_execution_mode(store, REAL, "core_satellite") == "PAPER"

    # 旧无前缀策略键优先于全局模式
    store.set_setting("strategy_execution_mode:core_satellite", "SHADOW")
    assert resolve_configured_execution_mode(store, PAPER, "core_satellite") == "SHADOW"

    # 租户策略键最高优先
    store.set_setting(f"strategy_execution_mode:{REAL}:core_satellite", "REAL")
    assert resolve_configured_execution_mode(store, REAL, "core_satellite") == "REAL"
    # 别的租户不受影响（隔离性）
    assert resolve_configured_execution_mode(store, PAPER, "core_satellite") == "SHADOW"
    # 逐策略覆盖只取显式值，不把全局模式混进来
    assert strategy_execution_mode_override(store, PAPER, "core_satellite") == "SHADOW"


def test_downgrade_writes_tenant_keys_not_only_legacy(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "t.db")
    monkeypatch.setattr(mode_mod, "active_tenants", lambda *a, **k: _fake_tenants())

    written = downgrade_strategy_execution_mode(store, "core_satellite")

    assert f"strategy_execution_mode:{REAL}:core_satellite" in written
    assert f"strategy_execution_mode:{PAPER}:core_satellite" in written
    for tenant in (REAL, PAPER):
        assert store.get_setting(
            f"strategy_execution_mode:{tenant}:core_satellite"
        ) == "SHADOW", f"{tenant} 没拿到降级键 —— 租户键会盖过旧键，降级失效"
    assert store.get_setting("strategy_execution_mode:core_satellite") == "SHADOW"


def test_restore_deletes_override_keys_instead_of_writing_paper(tmp_path, monkeypatch):
    store = TradingStore(tmp_path / "t.db")
    monkeypatch.setattr(mode_mod, "active_tenants", lambda *a, **k: _fake_tenants())
    downgrade_strategy_execution_mode(store, "core_satellite")

    removed = restore_strategy_execution_mode(store, "core_satellite", REAL)

    assert f"strategy_execution_mode:{REAL}:core_satellite" in removed
    assert store.get_setting(f"strategy_execution_mode:{REAL}:core_satellite") == ""
    # 绝不写 PAPER：写 PAPER 在实盘上会被白名单提升成 REAL
    assert store.get_setting(f"strategy_execution_mode:{REAL}:core_satellite") != "PAPER"
    # 旧全局键同样删除（否则它会继续压住「未降级」状态）
    assert store.get_setting("strategy_execution_mode:core_satellite") == ""
    # 另一个租户的降级不受影响（不允许跨租户顺手解禁）
    assert store.get_setting(f"strategy_execution_mode:{PAPER}:core_satellite") == "SHADOW"


def test_restore_keeps_legacy_key_when_other_tenant_relies_on_it(tmp_path, monkeypatch):
    """过渡期安全：旧全局键可能是别的租户**唯一**的压制来源，恢复单个租户时不能删。

    链路：升级前的自动降级只写旧键 → 生产里有「只有旧键 SHADOW、无租户键」的状态。
    若恢复 A 时无条件删旧键，B 会直接回到 REAL 下单，而没人点过恢复 B。
    变异：去掉「其余租户都有覆盖键才删旧键」判断（无条件删）→ 本用例转红。
    """
    store = TradingStore(tmp_path / "t.db")
    monkeypatch.setattr(mode_mod, "active_tenants", lambda *a, **k: _fake_tenants())
    # 过渡期形态：只有旧键 SHADOW，没有任何租户键
    store.set_setting("strategy_execution_mode:core_satellite", "SHADOW")

    removed = restore_strategy_execution_mode(store, "core_satellite", REAL)

    assert removed == [], "A 本来就没有租户键，不该删到任何东西"
    # 旧键必须保留：它是 B 唯一的压制来源
    assert store.get_setting("strategy_execution_mode:core_satellite") == "SHADOW"
    assert resolve_configured_execution_mode(store, PAPER, "core_satellite") == "SHADOW"


def test_restore_does_not_unsuppress_other_tenant(tmp_path, monkeypatch):
    """降级两个租户后只恢复 A，B 必须仍然 SHADOW。"""
    store = TradingStore(tmp_path / "t.db")
    monkeypatch.setattr(mode_mod, "active_tenants", lambda *a, **k: _fake_tenants())
    downgrade_strategy_execution_mode(store, "core_satellite")

    restore_strategy_execution_mode(store, "core_satellite", REAL)

    assert resolve_configured_execution_mode(store, PAPER, "core_satellite") == "SHADOW", (
        "恢复 A 把 B 的压制也解除了 —— 误解禁是危险方向"
    )


class _RealBroker:
    """实盘环境的极简 broker：只提供 submit_limit 解析模式所需字段。"""

    trading_env = "REAL"

    def __init__(self):
        self.placed = 0
        self.settings = SimpleNamespace(
            trading_env="REAL",
            real_allowed_strategies=("core_satellite",),
            real_require_manual_confirm=False,
        )

    def orders(self, historical=False):
        return pd.DataFrame(columns=["order_id", "order_status"])

    def positions(self):
        # 卖 1 股必须真有可用持仓，否则在模式判定之前就被「超卖」拦掉，
        # 测不到 SHADOW 分支。
        return pd.DataFrame([{
            "code": "US.SPY", "qty": 5, "market_val": 500,
            "nominal_price": 100, "cost_price": 100,
        }])

    def account_info(self):
        return pd.DataFrame([{"total_assets": 100000, "power": 100000}])

    def snapshots(self, symbols):
        return pd.DataFrame([
            {"code": symbol, "bid_price": 99, "ask_price": 101, "last_price": 100}
            for symbol in symbols
        ])

    def place_limit_order(self, symbol, side, quantity, price):
        self.placed += 1
        return pd.DataFrame([{"order_id": "should-not-happen"}])


def test_submit_limit_honors_tenant_shadow_override(tmp_path, monkeypatch):
    """降级键写到租户上，`submit_limit` 必须真的按 SHADOW 走（不下券商单）。

    这是「降级被架空」那条洞的正面守卫：租户键优先级最高，写错层就等于没降级。
    """
    broker = _RealBroker()
    store = TradingStore(tmp_path / "t.db")
    store.set_setting(f"strategy_execution_mode:{REAL}:core_satellite", "SHADOW")
    executor = ExecutionManager(broker, store, tenant=SimpleNamespace(
        tenant_id=REAL, trading_env="REAL", account_id="123456",
    ))

    result = executor.submit_limit(
        "core_satellite", "guard-shadow", "US.SPY", "BUY", 1, 100.0,
    )

    assert result.status == "SHADOW", f"租户 SHADOW 未生效，实际 {result.status}：{result.message}"
    assert broker.placed == 0, "SHADOW 模式下不允许调用券商下单"
