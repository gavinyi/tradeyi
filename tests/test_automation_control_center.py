from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text()


def test_navigation_names_single_strategy_capital_control():
    app = source("app.py")

    assert 'title="策略与资金控制"' in app
    assert 'title="策略控制台"' not in app


def test_automation_center_owns_service_radar_and_global_controls():
    control = source("pages/15_strategy_control_center.py")

    assert "from futu_quant.service_control import restart, start" in control
    assert "from futu_quant.radar_control import" in control
    assert 'store.set_setting("execution_mode"' in control
    assert 'st.subheader("服务控制")' in control
    assert 'st.subheader("策略资金")' in control
    assert "recommended_active_budget_rows" in control


def test_pause_all_strategies_is_tenant_scoped():
    """产品红线：暂停键必须按租户写，不得写全局键（否则跨租户连坐）。

    10-23 实锤：`run_service` 读取时先看租户键、再回落全局键，
    而控制页写的是全局键 —— 在实盘租户点「暂停全部策略」会连带停掉模拟盘，
    N-Tenant 隔离被一个全局开关整体绕过。
    """
    control = source("pages/15_strategy_control_center.py")

    assert "set_tenant_setting" in control, "暂停键未按租户写 —— 会跨租户连坐"
    # 写全局键只允许出现在「拿不到租户上下文」的兜底分支，且必须有注释标明
    assert 'store.set_setting("strategies_paused"' in control
    assert "fallback" in control, "全局兜底分支未标注"
    # 读取必须租户键优先
    assert "get_tenant_setting" in control, "读取未做租户键优先"


def test_control_snapshot_not_cached_across_tenant_switch():
    """快照含当前租户状态，不得缓存 —— 否则切换租户后仍显示上一个租户的状态。"""
    control = source("pages/15_strategy_control_center.py")
    idx = control.index("def load_control_snapshot")
    # 取该函数定义**之前**紧邻的若干行，缓存装饰器不得在其中
    preceding = control[max(0, idx - 200):idx]
    assert "@st.cache_data" not in preceding, (
        "load_control_snapshot 被缓存了 —— 切换租户后会显示上一个租户的状态"
    )


def test_system_monitor_is_read_only_and_links_to_control_center():
    monitor = source("pages/6_system_monitor.py")

    assert "from futu_quant.service_control import restart, start" not in monitor
    assert 'store.set_setting("execution_mode"' not in monitor
    assert 'store.set_setting("strategies_paused"' not in monitor
    assert "保存策略开关" not in monitor
    assert "st.page_link(" in monitor
    assert '"pages/15_strategy_control_center.py"' in monitor


def test_strategy_pages_do_not_write_runtime_switches():
    gamma = source("pages/5_earnings_gamma_strategy.py")
    volatility = source("pages/10_volatility_options_strategy.py")

    assert "开启 Gamma SHADOW 调试" not in gamma
    assert 'store.set_setting("strategy_enabled:Earnings Gamma"' not in gamma
    assert "开启波动率策略 SHADOW" not in volatility
    assert 'store.set_setting("strategy_enabled:Volatility Long Gamma"' not in volatility
    assert "st.page_link(" in gamma and '"pages/15_strategy_control_center.py"' in gamma
    assert "st.page_link(" in volatility and '"pages/15_strategy_control_center.py"' in volatility


def test_portfolio_risk_is_not_budget_editor_anymore():
    portfolio = source("pages/14_portfolio_risk.py")

    assert "保存三维策略预算" not in portfolio
    assert "st.data_editor(" not in portfolio
    assert "前往策略与资金控制" in portfolio
