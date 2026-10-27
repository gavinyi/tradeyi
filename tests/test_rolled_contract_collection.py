"""守卫：引擎 roll 出的「活跃合约」必须进入期权采集清单。

背景（2026-10-27 根因）：`volatility_optimizer` 每轮「期权表达复核」会把 campaign
roll 成新合约，新 code 不写回 campaigns 配置；而采集清单只读 campaigns 配置
（`option_history.py:23`）→ 新合约永远没有 `option_history` 行 → A14
「无历史行即 fail-closed」永远拦住入场（实盘/模拟有 BUY 却零成交的根因）。
"""

from futu_quant import option_history as oh


class FakeStore:
    def campaign_states(self) -> dict:
        return {
            "volatility_long_gamma:signal:futu_real_master:US.QQQ": {
                "active_contract_code": "US.QQQ261105C740000",
            },
            "volatility_long_gamma:signal:futu_paper_sandbox:US.NVDA": {
                "active_contract_code": "us.nvda261106p215000",
            },
            "core_satellite:futu_real_master:US.SPY": {
                "active_contract_code": "US.SPY261102C767000",
            },
            "earnings_gamma:US.AAPL": {
                "active_contract_code": "US.AAPL261120C200000",
            },
        }


def test_rolled_volatility_contract_is_collected(monkeypatch):
    monkeypatch.setattr(oh, "load_gamma_campaigns", lambda: [])
    monkeypatch.setattr(oh, "load_all_volatility_campaigns", lambda: [])
    codes = oh.configured_option_contracts(None, FakeStore())
    assert "US.QQQ261105C740000" in codes
    assert "US.NVDA261106P215000" in codes  # 大小写归一
    # 只并入 volatility_long_gamma 前缀的活跃合约，其它策略不扩采集范围
    assert "US.SPY261102C767000" not in codes
    assert "US.AAPL261120C200000" not in codes


def test_config_contracts_still_collected(monkeypatch):
    monkeypatch.setattr(
        oh, "load_gamma_campaigns",
        lambda: [{"code": "US.AAPL261120C200000", "legs": [{"code": "US.AAPL261120P190000"}]}],
    )
    monkeypatch.setattr(oh, "load_all_volatility_campaigns", lambda: [])
    codes = oh.configured_option_contracts(None, None)
    assert "US.AAPL261120C200000" in codes
    assert "US.AAPL261120P190000" in codes


def test_store_failure_does_not_break_collection(monkeypatch):
    class Boom:
        def campaign_states(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(oh, "load_gamma_campaigns", lambda: [])
    monkeypatch.setattr(oh, "load_all_volatility_campaigns", lambda: [])
    # 读状态失败 → 沿用配置清单（此处为空），绝不抛异常打断采集
    assert oh.configured_option_contracts(None, Boom()) == ()
