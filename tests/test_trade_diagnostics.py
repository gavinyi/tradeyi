from datetime import date, datetime, timezone

from futu_quant.trade_diagnostics import (
    build_diagnostic_report, diagnostic_markdown, diagnostic_timeline,
    diagnostic_window, latest_us_session_date,
)


class FakeStore:
    def decisions(self, limit=20_000):
        return [
            {
                "strategy_id": "volatility_scanner", "symbol": "US.AAPL",
                "decision": "BUY", "reason": "达到动态波动监测条件",
                "payload_json": "{}", "created_at": "2026-07-20T14:19:00+00:00",
            },
            {
                "strategy_id": "volatility_long_gamma",
                "symbol": "US.AAPL260724P335000", "decision": "BUY",
                "reason": "动态波动与方向确认后首次入场", "payload_json": "{}",
                "created_at": "2026-07-20T16:30:00+00:00",
            },
            {
                "strategy_id": "volatility_scanner", "symbol": "US.AMD",
                "decision": "CANDIDATE_BLOCKED", "reason": "模型预期收益未通过",
                "payload_json": "{}", "created_at": "2026-07-20T16:31:00+00:00",
            },
        ]

    def intents(self, limit=10_000):
        return [{
            "id": 26, "strategy_id": "core_satellite", "symbol": "US.SPY",
            "side": "SELL", "quantity": 145, "limit_price": 743.14,
            "status": "REJECTED", "error": "单笔名义金额超过 $20,000",
            "created_at": "2026-07-20T19:45:00+00:00",
        }]

    def audit_events(self, limit=20_000):
        return [{
            "strategy_id": "volatility_scanner",
            "event_type": "VOLATILITY_SHADOW_CAMPAIGN_AUTO_CREATED",
            "payload_json": '{"underlying":"US.AAPL","code":"US.AAPL260724P335000"}',
            "created_at": "2026-07-20T14:19:30+00:00",
        }]

    def shadow_fills(self, limit=20_000):
        return []


class FilledOptionStore(FakeStore):
    def decisions(self, limit=20_000):
        return []

    def intents(self, limit=10_000):
        return [{
            "id": 41, "strategy_id": "volatility_long_gamma",
            "symbol": "US.AAPL260724P335000", "side": "SELL", "quantity": 2,
            "limit_price": 7.25, "status": "FILLED", "error": "",
            "filled_quantity": 2, "average_fill_price": 7.2,
            "metadata_json": '{"multiplier":100}',
            "created_at": "2026-07-20T18:00:00+00:00",
        }]

    def audit_events(self, limit=20_000):
        return []


def test_latest_session_and_china_window_are_stable():
    now = datetime(2026, 7, 21, 2, tzinfo=timezone.utc)
    session = latest_us_session_date(now)
    window = diagnostic_window(session)

    assert session == date(2026, 7, 20)
    assert window.china_label == "2026-07-20 21:00–2026-07-21 08:30"


def test_report_exposes_signal_without_intent_and_core_risk_rejection():
    report = build_diagnostic_report(FakeStore(), diagnostic_window(date(2026, 7, 20)))
    summary = report["summary"]

    aapl = summary[
        (summary["策略"] == "volatility_long_gamma")
        & (summary["代码"] == "US.AAPL260724P335000")
    ].iloc[0]
    core = summary[
        (summary["策略"] == "core_satellite") & (summary["代码"] == "US.SPY")
    ].iloc[0]

    assert aapl["最终阶段"] == "交易前检查"
    assert aapl["最终结果"] == "未形成订单"
    assert core["最终阶段"] == "全局风控"
    assert core["最终结果"] == "已拒绝"
    assert core["交易方向"] == "卖出"
    assert core["计划/订单数量"] == 145
    assert core["限价"] == 743.14
    assert core["计划名义金额"] == 107755.3
    assert core["已成交数量"] == 0
    assert core["实际成交金额"] == 0
    assert report["metrics"]["触发标的"] == 1
    assert report["metrics"]["生成自动策略"] == 1
    assert "单笔名义金额超限" in report["blockers"]["未下单原因"].tolist()


def test_timeline_and_download_report_are_human_readable():
    report = build_diagnostic_report(FakeStore(), diagnostic_window(date(2026, 7, 20)))
    timeline = diagnostic_timeline(
        report, "volatility_long_gamma", "US.AAPL260724P335000",
    )
    markdown = diagnostic_markdown(report)

    assert timeline.iloc[0]["阶段"] == "策略决策"
    assert "动态波动与方向确认" in timeline.iloc[0]["说明"]
    assert "2026-07-20 美股交易诊断日报" in markdown
    assert "单笔名义金额超过" in markdown


def test_option_sell_reports_contract_multiplier_and_actual_fill_amount():
    report = build_diagnostic_report(
        FilledOptionStore(), diagnostic_window(date(2026, 7, 20)),
    )
    row = report["summary"].iloc[0]

    assert row["交易方向"] == "卖出"
    assert row["计划/订单数量"] == 2
    assert row["计划名义金额"] == 1450
    assert row["已成交数量"] == 2
    assert row["成交均价"] == 7.2
    assert row["实际成交金额"] == 1440
