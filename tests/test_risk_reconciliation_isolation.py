from __future__ import annotations

from types import SimpleNamespace

from futu_quant.reconciliation import reconcile_ledgers
from futu_quant.risk import RiskEngine, RiskLimits
from futu_quant.store import TradingStore


class MockBroker:
    def __init__(self, account_id="281756481476169109"):
        self.account_id = account_id
        self.trading_env = "REAL"
        self.settings = SimpleNamespace(
            trading_env="REAL",
            real_max_order_notional=10000.0,
            real_max_daily_notional=50000.0,
        )

    def positions(self):
        import pandas as pd
        return pd.DataFrame([{"code": "US.NVDA", "qty": 7.0}])

    def orders(self):
        import pandas as pd
        return pd.DataFrame()

    def account_info(self):
        import pandas as pd
        return pd.DataFrame([{"currency": "USD", "total_assets": 100000.0, "power": 50000.0, "unrealized_pl": 0.0}])

    def snapshots(self, symbols):
        import pandas as pd
        return pd.DataFrame([{"code": s, "bid_price": 100.0, "ask_price": 101.0, "last_price": 100.5} for s in symbols])


def test_fine_grained_reconciliation_risk_isolation(tmp_path):
    store = TradingStore(tmp_path / "risk.db")
    broker = MockBroker()
    risk = RiskEngine(store, RiskLimits(max_order_notional=10000.0, max_daily_notional=50000.0))

    # 1. 模拟股票策略发生对账差异：全局 recovery_blocked=true，且 core_satellite 被暂停
    store.set_setting("recovery_blocked", "true")
    store.set_setting("recovery_blocked:281756481476169109", "true")
    store.set_setting("strategy_reconciliation_disabled:core_satellite", "true")
    store.set_setting("symbol_blocked:281756481476169109:US.SPY", "true")

    # 2. 发生对账差异的策略 core_satellite 尝试买入 -> 必须精准阻断
    dec_strat_blocked = risk.check(
        broker, "US.AAPL", "BUY", 1, 150.0,
        strategy_id="core_satellite", execution_mode="REAL",
    )
    assert dec_strat_blocked.allowed is False
    assert "存在持仓差异已被对账暂停" in dec_strat_blocked.reason

    # 3. 发生对账差异的标的 US.SPY 尝试买入 -> 必须精准阻断
    dec_sym_blocked = risk.check(
        broker, "US.SPY", "BUY", 1, 400.0,
        strategy_id="other_strat", execution_mode="REAL",
    )
    assert dec_sym_blocked.allowed is False
    assert "存在持仓对账差异" in dec_sym_blocked.reason

    # 4. 重点：独立健康的期权策略 volatility_long_gamma 买入 AMD 期权 -> 绝不连坐误伤，必须正常放行！
    dec_option_pass = risk.check(
        broker, "US.AMD261030C560000", "BUY", 1, 15.0,
        multiplier=100.0, strategy_id="volatility_long_gamma", execution_mode="REAL",
    )
    assert dec_option_pass.allowed is True

    # 5. 平仓通道绝不阻断：即使处于对账差异策略中的持仓，SELL 平仓严禁被拦截！
    dec_sell_pass = risk.check(
        broker, "US.NVDA", "SELL", 1, 150.0,
        strategy_id="core_satellite", execution_mode="REAL",
    )
    assert dec_sell_pass.allowed is True

    # 6. 对账自愈闭环：差异修复后，对账标志清零，买入开仓立即自动恢复
    store.set_setting("strategy_reconciliation_disabled:core_satellite", "false")
    store.set_setting("symbol_blocked:281756481476169109:US.SPY", "false")
    dec_recovered = risk.check(
        broker, "US.SPY", "BUY", 1, 400.0,
        strategy_id="core_satellite", execution_mode="REAL",
    )
    assert dec_recovered.allowed is True


def test_reconciliation_isolation_fallback_without_strategy_id(tmp_path):
    """验证未指定具体策略 ID 时的全局阻断兜底行为与 SHADOW 豁免"""
    store = TradingStore(tmp_path / "risk_fallback.db")
    broker = MockBroker()
    risk = RiskEngine(store, RiskLimits(max_order_notional=10000.0, max_daily_notional=50000.0))

    store.set_setting("recovery_blocked", "true")

    # 未指定 strategy_id，触发全局兜底阻断
    dec_fallback = risk.check(
        broker, "US.AAPL", "BUY", 1, 150.0,
        strategy_id=None, execution_mode="REAL",
    )
    assert dec_fallback.allowed is False
    assert "启动恢复发现三账差异" in dec_fallback.reason

    # SHADOW 模式即便无 strategy_id 也不受阻断
    dec_shadow = risk.check(
        broker, "US.AAPL", "BUY", 1, 150.0,
        strategy_id=None, execution_mode="SHADOW",
    )
    assert dec_shadow.allowed is True


def test_reconciliation_isolation_tenant_id_prefix(tmp_path):
    """回归守卫：生产写入的是租户前缀键，broker 未携带 tenant_id 时也必须能阻断。

    生产链路：run_service.py:867 传 tenant_id -> reconciliation.py:93 写出
    `strategy_reconciliation_disabled:{tenant_id}:{strategy_id}`；
    而 FutuPaperBroker 没有 tenant_id 属性，risk.py 的租户前缀检查必须不依赖该属性，
    否则被对账暂停的策略仍能开新仓（拦多改成拦不住）。
    """
    store = TradingStore(tmp_path / "risk_tenant.db")
    # 生产口径 broker：无 tenant_id 属性
    broker = MockBroker()
    assert not hasattr(broker, "tenant_id"), "本用例必须用不携带 tenant_id 的生产口径 broker"
    risk = RiskEngine(store, RiskLimits(max_order_notional=10000.0, max_daily_notional=50000.0))

    # 生产实际写入格式：租户前缀键
    store.set_setting("strategy_reconciliation_disabled:futu_real_master:core_satellite", "true")

    dec_blocked = risk.check(
        broker, "US.AAPL", "BUY", 1, 150.0,
        strategy_id="core_satellite", execution_mode="REAL",
    )
    assert dec_blocked.allowed is False, "租户前缀键未能阻断被对账暂停的策略（生产回归）"
    assert "存在持仓差异已被对账暂停" in dec_blocked.reason


def test_reconciliation_isolation_account_prefix_blocks_and_isolates(tmp_path):
    """账户前缀键必须阻断本账户，且不误伤其他账户的同名策略。"""
    store = TradingStore(tmp_path / "risk_acct.db")
    broker = MockBroker(account_id="281756481476169109")
    risk = RiskEngine(store, RiskLimits(max_order_notional=10000.0, max_daily_notional=50000.0))

    store.set_setting("strategy_reconciliation_disabled:281756481476169109:core_satellite", "true")

    dec_blocked = risk.check(
        broker, "US.AAPL", "BUY", 1, 150.0,
        strategy_id="core_satellite", execution_mode="REAL",
    )
    assert dec_blocked.allowed is False

    # 其他账户的同名策略不受连坐
    broker_paper = MockBroker(account_id="20170229")
    dec_paper_pass = risk.check(
        broker_paper, "US.AAPL", "BUY", 1, 150.0,
        strategy_id="core_satellite", execution_mode="PAPER",
    )
    assert dec_paper_pass.allowed is True


def test_stale_account_prefixed_lock_is_cleared_on_recovery(tmp_path):
    """陈旧账户前缀锁（及其同族键）必须随对账恢复一并清理，否则策略被永久阻断。

    历史代码按 account 前缀写：
      strategy_reconciliation_disabled:{account}:{strategy} = true
      strategy_enabled:{account}:{strategy} = false
      strategy_reconciliation_restore:{account}:{strategy} = {...}
    新代码改按 tenant 前缀写；若恢复扫描只认当前前缀，
    旧前缀键永久孤儿化 —— 账本已平，但 risk.py:116 仍读到它，策略开不了新仓。
    """
    import json

    import pandas as pd

    class _FlatBroker:
        account_id = "281756481476169109"
        trading_env = "REAL"

        def positions(self):
            return pd.DataFrame()

    store = TradingStore(tmp_path / "stale_lock.db")
    broker = _FlatBroker()
    account_prefix_keys = {
        "strategy_reconciliation_disabled:281756481476169109:core_satellite": "true",
        "strategy_enabled:281756481476169109:core_satellite": "false",
        "strategy_reconciliation_restore:281756481476169109:core_satellite": json.dumps(
            {"strategy_enabled:281756481476169109:core_satellite": "true"}
        ),
    }
    for key, value in account_prefix_keys.items():
        store.set_setting(key, value)

    report = reconcile_ledgers(store, broker, tenant_id="futu_real_master")
    assert report[report["status"] == "MISMATCH"].empty

    assert store.get_setting(
        "strategy_reconciliation_disabled:281756481476169109:core_satellite", "false"
    ) == "false", "陈旧账户前缀禁用锁未清除，策略将被永久阻断"
    assert store.get_setting(
        "strategy_enabled:281756481476169109:core_satellite", "false"
    ) == "true", "陈旧账户前缀停用标记未按 restore 记录还原"
    assert store.get_setting(
        "strategy_reconciliation_restore:281756481476169109:core_satellite", ""
    ) == "", "陈旧账户前缀 restore 记录未清理"


def test_recovery_does_not_clear_other_tenants_real_lock(tmp_path):
    """本租户的恢复扫描绝不清别家租户的真实对账锁。

    恢复扫描若按「最后一段 == 策略名」匹配，实盘的对账会把模拟租户
    `futu_paper_sandbox:core_satellite=true` 这种**真实未平的锁**一并清掉，
    等于替别人放行。必须只认「无前缀」与「本租户已知前缀」两种形态。
    """
    import pandas as pd

    class _FlatBroker:
        account_id = "281756481476169109"
        trading_env = "REAL"

        def positions(self):
            return pd.DataFrame()

    store = TradingStore(tmp_path / "cross_tenant_lock.db")
    # 别家租户（模拟沙盒）的真实锁：那边账没平，不该被实盘的对账碰
    store.set_setting("strategy_reconciliation_disabled:futu_paper_sandbox:core_satellite", "true")
    store.set_setting("strategy_enabled:futu_paper_sandbox:core_satellite", "false")
    store.set_setting("strategy_reconciliation_restore:futu_paper_paper_sandbox:core_satellite", "")
    # 本租户（实盘）的陈旧账户前缀幽灵锁
    store.set_setting("strategy_reconciliation_disabled:281756481476169109:core_satellite", "true")
    store.set_setting("strategy_enabled:281756481476169109:core_satellite", "false")
    store.set_setting("strategy_reconciliation_restore:281756481476169109:core_satellite", "")

    report = reconcile_ledgers(store, _FlatBroker(), tenant_id="futu_real_master")
    assert report[report["status"] == "MISMATCH"].empty

    # 本租户的幽灵锁被清
    assert store.get_setting(
        "strategy_reconciliation_disabled:281756481476169109:core_satellite", "false"
    ) == "false"
    # 别家租户的真实锁必须原样保留
    assert store.get_setting(
        "strategy_reconciliation_disabled:futu_paper_sandbox:core_satellite", "false"
    ) == "true", "本租户对账清掉了别家租户的真实锁，等于替别人放行"
    assert store.get_setting(
        "strategy_enabled:futu_paper_sandbox:core_satellite", "true"
    ) == "false", "别家租户的停用标记被改动"


def test_recovery_is_idempotent(tmp_path):
    """恢复扫描必须幂等：连跑两次，第二次不得再改动任何键。"""
    import json

    import pandas as pd

    class _FlatBroker:
        account_id = "281756481476169109"
        trading_env = "REAL"

        def positions(self):
            return pd.DataFrame()

    store = TradingStore(tmp_path / "idempotent_lock.db")
    store.set_setting("strategy_reconciliation_disabled:281756481476169109:core_satellite", "true")
    store.set_setting("strategy_enabled:281756481476169109:core_satellite", "false")
    store.set_setting(
        "strategy_reconciliation_restore:281756481476169109:core_satellite",
        json.dumps({"strategy_enabled:281756481476169109:core_satellite": "true"}),
    )

    reconcile_ledgers(store, _FlatBroker(), tenant_id="futu_real_master")
    first = {
        key: store.get_setting(key, "")
        for key in (
            "strategy_reconciliation_disabled:281756481476169109:core_satellite",
            "strategy_enabled:281756481476169109:core_satellite",
            "strategy_reconciliation_restore:281756481476169109:core_satellite",
        )
    }
    reconcile_ledgers(store, _FlatBroker(), tenant_id="futu_real_master")
    second = {
        key: store.get_setting(key, "")
        for key in first
    }
    assert second == first, f"恢复扫描不幂等：{first} -> {second}"
    assert first["strategy_enabled:281756481476169109:core_satellite"] == "true"


def test_recovery_restores_user_disabled_tenant_strategy(tmp_path):
    """守卫：暂停前由用户主动停用的租户键，恢复后必须仍是停用。

    `_disable_strategy_for_reconciliation` 的 restore 快照只记无前缀键；
    若带前缀恢复读不到原值而回落 "true"，会把用户主动停用的策略**静默改回启用**。
    """
    from futu_quant.reconciliation import (
        _disable_strategy_for_reconciliation,
        _recover_strategy_from_reconciliation,
    )

    store = TradingStore(tmp_path / "restore_disabled.db")
    # 用户主动停用（租户键为 false），全局键为 true
    store.set_setting("strategy_enabled:futu_real_master:core_satellite", "false")
    store.set_setting("strategy_enabled:Core Satellite", "true")

    _disable_strategy_for_reconciliation(store, "core_satellite", prefix="futu_real_master:")
    assert store.get_setting(
        "strategy_reconciliation_disabled:futu_real_master:core_satellite", "false"
    ) == "true"

    _recover_strategy_from_reconciliation(store, "core_satellite", prefix="futu_real_master:")
    assert store.get_setting(
        "strategy_enabled:futu_real_master:core_satellite", "false"
    ) == "false", "用户主动停用的策略被对账恢复静默改回启用"


def test_recovery_restores_both_prefixed_key_forms(tmp_path):
    """守卫：带前缀的两种拼法都必须进 restore 快照，恢复后都回到暂停前的值。

    只断言单一拼法会漏：代码写的是 `{prefix}{strategy_id}`，
    而历史读侧用 `{prefix}{service_name}`；两种都要覆盖，否则总有一种被静默改写。
    """
    from futu_quant.reconciliation import (
        _disable_strategy_for_reconciliation,
        _recover_strategy_from_reconciliation,
    )

    store = TradingStore(tmp_path / "restore_both_forms.db")
    # 用户按两种拼法都显式停用
    store.set_setting("strategy_enabled:futu_real_master:core_satellite", "false")
    store.set_setting("strategy_enabled:futu_real_master:Core Satellite", "false")
    store.set_setting("strategy_enabled:Core Satellite", "true")

    _disable_strategy_for_reconciliation(store, "core_satellite", prefix="futu_real_master:")
    _recover_strategy_from_reconciliation(store, "core_satellite", prefix="futu_real_master:")

    assert store.get_setting(
        "strategy_enabled:futu_real_master:core_satellite", "true"
    ) == "false", "策略 ID 拼法的租户键被恢复逻辑覆盖"
    assert store.get_setting(
        "strategy_enabled:futu_real_master:Core Satellite", "true"
    ) == "false", "显示名拼法的租户键被恢复逻辑覆盖"


def test_recovery_falls_back_to_enabled_only_without_snapshot_record(tmp_path):
    """守卫（已反转）：快照记录为空 = pause 前该键不存在 = 恢复后必须保持未设置。

    ⚠️ 本用例原先把缺陷写成了预期（断言"无快照记录时回落 true"），
    等于守护一个 bug —— 已按 Chief 裁定反转。读侧 `if candidate:` 视空为未设置，
    回落全局键，语义不变。
    """
    from futu_quant.reconciliation import (
        _disable_strategy_for_reconciliation,
        _recover_strategy_from_reconciliation,
    )

    store = TradingStore(tmp_path / "restore_fallback.db")
    # 暂停前租户键完全不存在（用户从未声明过），快照记的是空串
    store.set_setting("strategy_enabled:core_satellite", "false")

    _disable_strategy_for_reconciliation(store, "core_satellite", prefix="futu_real_master:")
    _recover_strategy_from_reconciliation(store, "core_satellite", prefix="futu_real_master:")

    assert store.get_setting(
        "strategy_enabled:futu_real_master:core_satellite", ""
    ) in ("", None), "快照为空记录时把租户键凭空钉成启用，改变了对账未观察过的启用状态"


def test_recovery_keeps_absent_tenant_key_absent(tmp_path):
    """守卫（读侧可见结果）：对账暂停→恢复不得改变任何策略的调度判定。

    ① pause 前与 recover 后，`resolve_configured_strategies` 必须返回**同一个集合**；
    ② pause 前不存在的租户键，recover 后必须依然不存在（不得被凭空钉成 "true"）。

    根因：`restore.get(key, "true") or "true"` 把「快照里没有这个键」和
    「快照记录它原本是空的」混为一谈——后者说明 pause 前该键不存在，
    正确做法是恢复成「不存在」（读侧照旧回落全局键），而不是钉成 true。
    """
    import sys
    from pathlib import Path

    from futu_quant.reconciliation import (
        _disable_strategy_for_reconciliation,
        _recover_strategy_from_reconciliation,
    )

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from run_service import resolve_configured_strategies  # noqa: E402

    store = TradingStore(tmp_path / "restore_absent.db")
    # 租户键完全不存在；全局键为 false -> 读侧判定未启用
    store.set_setting("strategy_enabled:Core Satellite", "false")

    # ① pause 前的读侧判定
    before = resolve_configured_strategies(store, "futu_real_master", {"Core Satellite"})

    _disable_strategy_for_reconciliation(store, "core_satellite", prefix="futu_real_master:")
    _recover_strategy_from_reconciliation(store, "core_satellite", prefix="futu_real_master:")

    # ① recover 后必须完全一致
    after = resolve_configured_strategies(store, "futu_real_master", {"Core Satellite"})
    assert after == before, f"对账恢复改变了调度判定：{before} -> {after}"
    assert "Core Satellite" not in after, "恢复后读侧判定从「未启用」变成「启用」"

    # ② 不存在的键必须依然不存在
    value = store.get_setting("strategy_enabled:futu_real_master:core_satellite", "")
    assert value in ("", None), f"未设置的租户键被凭空钉成 {value!r}，策略被静默改回启用"


def test_disable_pauses_all_enable_key_forms(tmp_path):
    """守卫：对账暂停必须把该变体的**全部**启用键形式置为 false。

    读侧显示名优先、蛇形兜底（先取到非空就 break）；若写侧只写蛇形键，
    只要显示名键存在且为 true，暂停就**静默失效**（策略照样被调度）。
    这条同时守住重启清单第 ④ 项——那枚显示名键一旦被显式写入，
    必须能被对账暂停一并关掉。
    """
    import sys
    from pathlib import Path

    from futu_quant.reconciliation import _disable_strategy_for_reconciliation

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from run_service import resolve_configured_strategies  # noqa: E402

    store = TradingStore(tmp_path / "pause_all_forms.db")
    # 显示名键存在且为 true，蛇形键不存在（重启清单第 ④ 项要写入的就是这种状态）
    store.set_setting("strategy_enabled:futu_real_master:Core Satellite", "true")

    before = resolve_configured_strategies(store, "futu_real_master", {"Core Satellite"})
    assert "Core Satellite" in before, "前置条件不成立：显示名键未生效"

    _disable_strategy_for_reconciliation(store, "core_satellite", prefix="futu_real_master:")

    assert store.get_setting(
        "strategy_enabled:futu_real_master:Core Satellite", "true"
    ) == "false", "暂停未关掉显示名形式的启用键"
    assert store.get_setting(
        "strategy_enabled:futu_real_master:core_satellite", "true"
    ) == "false", "暂停未关掉蛇形形式的启用键"

    after = resolve_configured_strategies(store, "futu_real_master", {"Core Satellite"})
    assert "Core Satellite" not in after, "暂停静默失效：策略仍被调度"


def test_recover_does_not_create_snake_key_when_only_display_name_existed(tmp_path):
    """守卫（与 delete_setting 那枚成对）：只设了显示名键时，恢复不得凭空创建蛇形键。

    修「写侧覆盖两种拼法」时容易踩回来的坑：disable 现在两种拼法都置 false，
    若恢复按「统一还原成 true」处理，就会把从未存在过的蛇形键创建出来。
    """
    from futu_quant.reconciliation import (
        _disable_strategy_for_reconciliation,
        _recover_strategy_from_reconciliation,
    )

    store = TradingStore(tmp_path / "no_snake_creation.db")
    store.set_setting("strategy_enabled:futu_real_master:Core Satellite", "true")
    # 蛇形键从未存在
    assert store.get_setting("strategy_enabled:futu_real_master:core_satellite", "") == ""

    _disable_strategy_for_reconciliation(store, "core_satellite", prefix="futu_real_master:")
    _recover_strategy_from_reconciliation(store, "core_satellite", prefix="futu_real_master:")

    # 显示名键按快照还原成 true
    assert store.get_setting(
        "strategy_enabled:futu_real_master:Core Satellite", "false"
    ) == "true", "显示名键未按快照还原"
    # 蛇形键必须仍不存在（不得被凭空创建）
    assert store.get_setting(
        "strategy_enabled:futu_real_master:core_satellite", ""
    ) in ("", None), "恢复凭空创建了从未存在过的蛇形键"


def test_quick_launch_writes_tenant_scoped_enable_key():
    """守卫：UI「一键上线」必须写租户级启用键，且不得写全局键（写侧是读侧的超集）。

    读侧租户键优先于全局键（先取到非空即 break）：只写全局键时，若租户键被显式
    置 false（用户主动停用），按钮会报「已启用」而策略实际仍不调度 —— 静默失效；
    写全局键还会污染另一个租户的回落值（跨租户写入）。

    键可能是常量、f-string（租户级必然含租户变量），也可能先放进元组再循环写
    （调用传的是循环变量名）——三种形态都要能取到，否则守卫会空转。
    """
    import ast
    from pathlib import Path

    source = Path("pages/9_launch_approval.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    def _is_enable_key(node) -> bool:
        return isinstance(node, (ast.Constant, ast.JoinedStr)) and "strategy_enabled:" in ast.unparse(node)

    keys: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_setting" and node.args):
            continue
        arg = node.args[0]
        if _is_enable_key(arg):
            keys.append(ast.unparse(arg))
        elif isinstance(arg, ast.Name):
            # for _key in (...): set_setting(_key, ...) —— 键在循环可迭代对象里
            for loop in ast.walk(tree):
                if (isinstance(loop, ast.For) and isinstance(loop.target, ast.Name)
                        and loop.target.id == arg.id
                        and isinstance(loop.iter, (ast.Tuple, ast.List))):
                    keys.extend(ast.unparse(elt) for elt in loop.iter.elts if _is_enable_key(elt))

    assert keys, "未找到 strategy_enabled 写入点（常量 / f-string / 循环元组三种形态都要能取到）"
    # 正向：租户级两种拼法都必须写
    assert any("_tenant_id" in k and "Core Satellite" in k for k in keys), (
        f"未写租户级显示名启用键: {keys}"
    )
    assert any("_tenant_id" in k and "core_satellite" in k for k in keys), (
        f"未写租户级蛇形启用键: {keys}"
    )
    # 反向：不得写无前缀的全局键
    assert not any(
        ":Core Satellite" in k and "_tenant_id" not in k and ":core_satellite" not in k
        for k in keys
    ), f"仍写了无前缀全局键（会污染其他租户的回落值）: {keys}"


def test_alert_manager_whitelisted_ledger_mismatch_not_swallowed_when_incident_already_open(tmp_path):
    """
    防静默熔断用例：
    验证即便事故早已处于 OPEN 状态，白名单致命事件（如 LEDGER_MISMATCH）调用 alerts.send 时
    绝不会在第一毫秒被 return 拦截吞噬，必须允许向下派发告警与卡片。
    """
    from futu_quant.alerts import AlertManager
    store = TradingStore(tmp_path / "alerts_anti_silence.db")
    alerts = AlertManager(store)

    # 1. 模拟对账底层抢先写入了 OPEN 状态的 incident
    store.record_incident("LEDGER_MISMATCH", "ERROR", "底层对账差异预先记录")

    # 2. 模拟外部调度器再次调用 alerts.send，验证不会被提前 return 拦截，必须生成 alert 记录
    alerts.send("ERROR", "LEDGER_MISMATCH", "发现持仓差异，策略已暂停")

    # 3. 校验 alert 已成功落盘
    recent = store.alerts(limit=5)
    assert len(recent) > 0
    assert any(a["event_type"] == "LEDGER_MISMATCH" for a in recent)


def test_alert_manager_option_lifecycle_risk_is_whitelisted(tmp_path):
    """
    防静默用例（真实缺口修复）：
    OPTION_LIFECYCLE_RISK 曾在白名单之外，导致「期权临近到期将产生裸空正股敞口」
    这类必须人工处置的风险长期 delivered=0（仅落库、从不推送）。本用例锁定该事件
    必须在白名单内，且事故已 OPEN 时也不会被提前 return 吞掉。
    """
    from futu_quant.alerts import FEISHU_GROUP_ALERT_WHITELIST, AlertManager

    assert "OPTION_LIFECYCLE_RISK" in FEISHU_GROUP_ALERT_WHITELIST, (
        "OPTION_LIFECYCLE_RISK 必须留在飞书白名单内，否则裸空敞口风险会被静默"
    )

    store = TradingStore(tmp_path / "alerts_lifecycle_risk.db")
    alerts = AlertManager(store)
    # 事故预先处于 OPEN（模拟重复触发场景）
    store.record_incident(
        "OPTION_LIFECYCLE_RISK", "ERROR", "剩余 6 天，价内；预计交割正股缺口 100 股"
    )
    alerts.send(
        "ERROR", "OPTION_LIFECYCLE_RISK",
        "US.AMZN261028P252500：剩余 6 天，价内，每份内在价值 $0.49；预计交割正股缺口 100 股",
    )
    recent = store.alerts(limit=5)
    assert any(a["event_type"] == "OPTION_LIFECYCLE_RISK" for a in recent), (
        "白名单事件在事故已 OPEN 时不得被提前 return 吞噬"
    )



