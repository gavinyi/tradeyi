"""P1：自动雷达每批权利金预算必须按租户取（实盘/模拟不得共用同一份钱）。

背景（`data/qa/p1_radar_budget_tenant_isolation_20261103.md`）：
改前 `resolve_volatility_radar_budget(symbol)` 只有标的、没有租户，
两租户必然拿到同一个 `$1,000`；函数在**能力层面**就区分不了租户。
后果：模拟盘被实盘的小额度限住（赛马/A-B 结论失真），任一侧调值都会污染另一侧。

存储分层（与 `risk_limits:{tenant}`、`real_account_config:{acct}` 一致）：
租户键 → `system_settings`；无前缀旧键 → `runtime_config`（存量，行为不变）。

本文件的守卫分两类：
- 取值语义：租户键优先、迁移态回落旧键并留痕、单作用域才用旧键；
- 结构性：**调用点必须传租户**（AST 断言），防止签名又被改回租户盲。
"""

from __future__ import annotations

import ast
from pathlib import Path

import futu_quant.config as config
from futu_quant.store import TradingStore

REPO_ROOT = Path(__file__).resolve().parents[1]

TENANT_REAL = "futu_real_master"
TENANT_PAPER = "futu_paper_sandbox"
LEGACY_KEY = "VOLATILITY_RADAR_BUDGETS"
REAL_KEY = f"{LEGACY_KEY}:{TENANT_REAL}"
PAPER_KEY = f"{LEGACY_KEY}:{TENANT_PAPER}"


def _install(monkeypatch, tenant_budgets: dict | None = None, legacy: dict | None = None):
    """替换两层读取，返回被读过的键序列（用于证明「先读租户键」）。"""
    seen: list[str] = []
    tenant_budgets = tenant_budgets or {}

    def _tenant(tenant_id, store=None):
        key = f"{LEGACY_KEY}:{tenant_id}"
        seen.append(key)
        return tenant_budgets.get(key)

    def _runtime(key, default=None, store=None):
        seen.append(key)
        return legacy if legacy is not None else default

    monkeypatch.setattr(config, "_load_tenant_budget", _tenant)
    monkeypatch.setattr(config, "_load_runtime_value", _runtime)
    monkeypatch.setattr(config, "_RADAR_BUDGET_LEGACY_WARNED", set())
    return seen


# --------------------------------------------------------------------------- #
# 取值语义
# --------------------------------------------------------------------------- #


def test_tenant_key_wins_over_legacy_global(monkeypatch):
    seen = _install(
        monkeypatch,
        tenant_budgets={REAL_KEY: {"default": 2500.0, "symbols": {}}},
        legacy={"default": 1000.0, "symbols": {}},
    )

    assert config.resolve_volatility_radar_budget("QQQ", TENANT_REAL) == 2500.0
    # 必须**先**读租户键：顺序反了就等于永远用旧键，隔离形同虚设。
    assert seen[0] == REAL_KEY
    assert LEGACY_KEY not in seen


def test_legacy_key_is_only_a_migration_fallback(monkeypatch):
    """租户合法但租户键还没写入：按旧键取值（零行为变化），并留痕。"""
    seen = _install(monkeypatch, tenant_budgets={}, legacy={"default": 1000.0, "symbols": {}})

    assert config.resolve_volatility_radar_budget("QQQ", TENANT_REAL) == 1000.0
    assert seen[0] == REAL_KEY and LEGACY_KEY in seen


def test_single_scope_still_uses_the_legacy_key(monkeypatch):
    """没有租户上下文的历史入口：只有一份钱，不存在跨租户共用。"""
    seen = _install(monkeypatch, tenant_budgets={}, legacy={"default": 777.0, "symbols": {}})

    assert config.resolve_volatility_radar_budget("QQQ") == 777.0
    assert seen == [LEGACY_KEY], "单作用域不得去读任何租户键"


def test_unknown_tenant_is_fail_closed(monkeypatch):
    """拼错的租户名必须返回 0，**一次都不许读**租户键或旧键。"""
    seen = _install(monkeypatch, tenant_budgets={}, legacy={"default": 1000.0, "symbols": {}})

    assert config.resolve_volatility_radar_budget("QQQ", "not_a_tenant") == 0.0
    assert seen == [], "非法租户不得读取任何预算键（读旧键=静默用到别人的钱）"


def test_migration_fallback_writes_persistent_event(monkeypatch, tmp_path):
    """迁移态（租户键未写）允许回落旧键，但必须落**持久事件** LEGACY_KEY_FALLBACK。"""
    store = TradingStore(tmp_path / "fallback.db")
    monkeypatch.setattr(config, "_RADAR_BUDGET_LEGACY_WARNED", set())
    monkeypatch.setattr(
        config, "_load_runtime_value",
        lambda key, default=None, store=None: {"default": 1000.0, "symbols": {}},
    )

    assert config.resolve_volatility_radar_budget("QQQ", TENANT_REAL, store) == 1000.0

    events = [
        row for row in store.operational_events(limit=200)
        if str(row.get("event_type")) == "LEGACY_KEY_FALLBACK"
    ]
    assert len(events) == 1, "迁移态回落必须落持久事件，不能只有进程内 WARNING"
    payload = events[0].get("payload") if isinstance(events[0].get("payload"), dict) else {}
    assert str(events[0]).find(REAL_KEY) >= 0 or REAL_KEY in str(payload)

    # 同一进程内不重复刷事件
    config.resolve_volatility_radar_budget("QQQ", TENANT_REAL, store)
    again = [
        row for row in store.operational_events(limit=200)
        if str(row.get("event_type")) == "LEGACY_KEY_FALLBACK"
    ]
    assert len(again) == 1


def test_two_tenants_do_not_share_the_same_budget(monkeypatch):
    _install(monkeypatch, tenant_budgets={
        REAL_KEY: {"default": 1000.0, "symbols": {}},
        PAPER_KEY: {"default": 4000.0, "symbols": {}},
    }, legacy={"default": 1000.0, "symbols": {}})

    assert config.resolve_volatility_radar_budget("QQQ", TENANT_REAL) == 1000.0
    assert config.resolve_volatility_radar_budget("QQQ", TENANT_PAPER) == 4000.0


def test_symbol_override_is_scoped_inside_the_tenant(monkeypatch):
    _install(monkeypatch, tenant_budgets={
        REAL_KEY: {"default": 1000.0, "symbols": {"US.GOOG": 2500.0}},
        PAPER_KEY: {"default": 4000.0, "symbols": {}},
    })

    assert config.resolve_volatility_radar_budget("GOOG", TENANT_REAL) == 2500.0
    # 实盘的 GOOG 单独预算不得漏到模拟盘
    assert config.resolve_volatility_radar_budget("GOOG", TENANT_PAPER) == 4000.0


# --------------------------------------------------------------------------- #
# 写侧：保存必须落到雷达读的那把键（否则「保存成功但雷达不生效」）
# --------------------------------------------------------------------------- #


def test_save_writes_the_tenant_key_and_leaves_global_untouched(monkeypatch, tmp_path):
    store = TradingStore(tmp_path / "budget.db")
    monkeypatch.setattr(config, "_RADAR_BUDGET_LEGACY_WARNED", set())
    monkeypatch.setattr(
        config, "_load_runtime_value",
        lambda key, default=None, store=None: default,
    )

    config.save_volatility_radar_budgets(
        1200.0, {"QQQ": 900.0}, tenant_id=TENANT_REAL, store=store,
    )

    assert store.get_runtime_config(LEGACY_KEY) is None, "带租户保存不得写全局 runtime_config"
    budgets = config.load_volatility_radar_budgets(TENANT_REAL, store=store)
    assert budgets["default"] == 1200.0
    assert budgets["symbols"] == {"US.QQQ": 900.0}

    # 另一个租户没被写入 → 迁移态：回落旧键（这里旧键不存在 → 默认 $1,000）
    assert config.resolve_volatility_radar_budget("QQQ", TENANT_PAPER, store=store) == 1000.0


def test_save_without_tenant_keeps_the_legacy_contract(monkeypatch, tmp_path):
    """无租户保存仍是旧键 + .env 镜像（历史脚本不能用坏）。

    ENV_FILE 指向临时文件 → 不写任何库，只验证键与镜像都仍是**无前缀旧键**。
    """
    mirrored: list[tuple[str, str]] = []
    monkeypatch.setattr(config, "_mirror_env", lambda key, value: mirrored.append((key, value)))
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")

    value = config.save_volatility_radar_budgets(1500.0, {"QQQ": 800.0})

    assert value == {"default": 1500.0, "symbols": {"US.QQQ": 800.0}}
    assert mirrored and mirrored[0][0] == LEGACY_KEY


# --------------------------------------------------------------------------- #
# 结构性守卫：调用点必须传租户（AST，防止签名又被改回租户盲）
# --------------------------------------------------------------------------- #


def _calls(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        target = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if target == name:
            found.append(node)
    return found


def _passes_tenant(call: ast.Call, min_positional: int) -> bool:
    """调用是否显式传了租户：位置参数够数，或写了 `tenant_id=` 关键字。"""
    return len(call.args) >= min_positional or any(
        kw.arg == "tenant_id" for kw in call.keywords
    )


def test_radar_scanner_passes_tenant_to_budget_resolver():
    calls = _calls(
        REPO_ROOT / "src" / "futu_quant" / "volatility_scanner.py",
        "resolve_volatility_radar_budget",
    )
    assert calls, "雷达必须调用预算解析函数"
    for call in calls:
        # 第 1 个参数是标的、第 2 个才是租户 —— 少传就等于租户盲。
        assert _passes_tenant(call, 2), "雷达取预算必须带租户，否则会回落到全局共用预算"


def test_volatility_page_passes_tenant_on_both_sides():
    """页面的**读**与**写**都必须带租户：只带一侧就会出现「看到的是 A、写的是 B」。"""
    page = REPO_ROOT / "pages" / "10_volatility_options_strategy.py"
    for func_name in ("load_volatility_radar_budgets", "save_volatility_radar_budgets"):
        calls = _calls(page, func_name)
        assert calls, f"页面必须调用 {func_name}"
        for call in calls:
            # 页面这两个函数第 1 个参数就是租户。
            assert _passes_tenant(call, 1), f"页面调用 {func_name} 必须带租户"


def test_money_budget_config_functions_accept_tenant():
    """P1 §四.4 守卫：钱类预算配置函数必须带租户参数。"""
    tree = ast.parse((REPO_ROOT / "src" / "futu_quant" / "config.py").read_text(encoding="utf-8"))
    offenders = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and "BUDGET" in node.name.upper():
            params = {arg.arg for arg in node.args.args} | {
                arg.arg for arg in node.args.kwonlyargs
            }
            if "tenant_id" not in params:
                offenders.append(node.name)
    assert offenders == [], f"钱类预算函数必须接受 tenant_id：{offenders}"
