from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text()


def test_navigation_names_single_strategy_capital_control():
    app = source("app.py")

    assert 'title="策略与资金控制"' in app
    assert 'title="策略控制台"' not in app


def test_automation_center_owns_service_radar_and_global_controls():
    control = source("pages/15_策略控制台.py")

    assert "from futu_quant.service_control import restart, start" in control
    assert "from futu_quant.radar_control import" in control
    assert 'store.set_setting("execution_mode"' in control
    assert 'store.set_setting("strategies_paused"' in control
    assert 'st.subheader("服务控制")' in control
    assert 'st.subheader("策略资金")' in control
    assert "recommended_active_budget_rows" in control


def test_system_monitor_is_read_only_and_links_to_control_center():
    monitor = source("pages/6_系统监控.py")

    assert "from futu_quant.service_control import restart, start" not in monitor
    assert 'store.set_setting("execution_mode"' not in monitor
    assert 'store.set_setting("strategies_paused"' not in monitor
    assert "保存策略开关" not in monitor
    assert "st.page_link(" in monitor
    assert '"pages/15_策略控制台.py"' in monitor


def test_strategy_pages_do_not_write_runtime_switches():
    gamma = source("pages/5_财报Gamma策略.py")
    volatility = source("pages/10_波动率期权策略.py")

    assert "开启 Gamma SHADOW 调试" not in gamma
    assert 'store.set_setting("strategy_enabled:Earnings Gamma"' not in gamma
    assert "开启波动率策略 SHADOW" not in volatility
    assert 'store.set_setting("strategy_enabled:Volatility Long Gamma"' not in volatility
    assert "st.page_link(" in gamma and '"pages/15_策略控制台.py"' in gamma
    assert "st.page_link(" in volatility and '"pages/15_策略控制台.py"' in volatility


def test_portfolio_risk_is_not_budget_editor_anymore():
    portfolio = source("pages/14_组合风险.py")

    assert "保存三维策略预算" not in portfolio
    assert "st.data_editor(" not in portfolio
    assert "前往策略与资金控制" in portfolio
