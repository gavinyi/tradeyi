"""退出决策的证据留痕 + 拦截告警的定位字段（2026-10-30 复盘缺口）。

两件事：

**一、复盘只能反推**

13:51:56 硬止损的 decision payload 只有
    {"position_pnl_amount": -75.0, "pnl_ratio": -0.0819672, ...}
没有 bid / ask / mid。判定价 8.40 能从 `915-75=840` 反推，但**卖一反推不出来**，
于是「当时 mid 是 8.60 还是 8.70」无法证实 —— 而这个数直接决定
「按 mid 判定会不会触发」。风控决策必须自带证据。

**二、告警认不出是谁**

10-30 COIN 的 18 条 `VOLATILITY_STOP_LIMIT_MISSING`，payload 里
`campaign_id: null` 且**完全没有 tenant_id**（seed 不写 id，details 不带你租户）。
运维只能拿 decision_logs 反查。

本文件用 AST 断言把「details 必须带 tenant_id」钉在源码上 —— 这类字段一旦
在后续重构里被顺手删掉，单测看不出来，只有复盘时才发现又缺了。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _load_gamma():
    import run_gamma_strategy as module

    return module


# ---------------------------------------------------------------- 盘口快照


def test_exit_quote_snapshot_carries_full_book():
    rg = _load_gamma()
    quote_check = {
        "spread_pct": 5.5,
        "option_age_seconds": 101.0,
        "trade_age_seconds": 101.0,
        "option_fresh": True,
        "option_degraded": False,
        "book_reason": "盘口可用：买一 8.40、卖一 9.00、价差 6.9%",
        "quote_source": "FUTU_OPEND_SNAPSHOT",
        "option_source_time": "2026-10-30 09:50:15",
        "captured_at": "2026-10-30T13:51:55.123+00:00",
        "attempts": [{"attempt": 1}],
    }

    snapshot = rg._exit_quote_snapshot(
        quote_check, bid=8.40, ask=9.00, last=8.40, mid=8.70,
    )

    assert snapshot["bid"] == pytest.approx(8.40)
    assert snapshot["ask"] == pytest.approx(9.00), (
        "卖一必须留痕 —— 13:51:56 那笔就是因为没记 ask，mid 只能靠猜"
    )
    assert snapshot["mid"] == pytest.approx(8.70)
    assert snapshot["spread_pct"] == pytest.approx(5.5)
    assert snapshot["option_age_seconds"] == pytest.approx(101.0)
    assert snapshot["fresh"] is True
    assert snapshot["retries"] == 1
    assert snapshot["captured_at"].startswith("2026-10-30T13:51:55")


def test_exit_quote_snapshot_tolerates_missing_quote_check():
    """quote_check 缺失时不能抛 —— 退出决策优先，留痕是旁路。"""
    rg = _load_gamma()

    snapshot = rg._exit_quote_snapshot(None, bid=0.0, ask=0.0, last=7.0, mid=7.0)

    assert snapshot["bid"] == 0.0
    assert snapshot["fresh"] is False
    assert snapshot["retries"] == 0


def test_hard_stop_sell_path_embeds_exit_quote():
    """硬止损 decision 必须带 exit_quote（AST 级，防止重构时被删）。"""
    tree = ast.parse((ROOT / "scripts" / "run_gamma_strategy.py").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "decision"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "strategy_id"
        and len(node.args) > 2
        and isinstance(node.args[2], ast.Constant)
        and node.args[2].value == "SELL"
    ]
    assert calls, "没找到硬止损的 SELL decision 调用 —— 结构变了，请同步本守卫"
    payloads = [c.args[4] for c in calls if len(c.args) > 4]
    keys = {
        key.value
        for payload in payloads
        if isinstance(payload, ast.Dict)
        for key in payload.keys
        if isinstance(key, ast.Constant)
    }
    assert "exit_quote" in keys, (
        "硬止损 SELL 决策里没有 exit_quote —— 又会退回到「判定价只能反推」的状态"
    )
    assert "hard_stop_basis" in keys, "判定口径（MID/BID）也必须留痕"


# ------------------------------------------------------------ 拦截告警定位


def _pretrade_block_details(check: str) -> list[dict[str, ast.AST]]:
    """取出所有 `record_pretrade_block(<check>, ..., {...})` 的第三个实参。"""
    tree = ast.parse((ROOT / "scripts" / "run_gamma_strategy.py").read_text(encoding="utf-8"))
    found: list[dict[str, ast.AST]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "record_pretrade_block"
        ):
            continue
        if len(node.args) < 3 or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != check:
            continue
        payload = node.args[2]
        if isinstance(payload, ast.Dict):
            found.append({
                key.value: value
                for key, value in zip(payload.keys, payload.values)
                if isinstance(key, ast.Constant)
            })
    return found


@pytest.mark.parametrize(
    "check",
    ["VOLATILITY_STOP_LIMIT_MISSING", "VOLATILITY_STOP_LIMIT_INVALID"],
)
def test_stop_limit_block_payload_has_locators(check: str):
    details = _pretrade_block_details(check)

    assert details, f"{check} 的拦截 payload 没找到 —— 结构变了，请同步本守卫"
    for payload in details:
        assert "tenant_id" in payload, (
            f"{check} 的 payload 缺 tenant_id —— 10-30 那 18 条拦截就是因为没有它，"
            f"运维认不出是哪个租户被拦"
        )
        assert "campaign_id" in payload
        assert "underlying" in payload, "按标的聚合告警需要它"


def test_stop_limit_alert_incident_key_is_tenant_scoped():
    """告警 incident_key 必须按租户隔离，否则两个租户互相抑制。"""
    source = (ROOT / "scripts" / "run_gamma_strategy.py").read_text(encoding="utf-8")

    assert 'incident_key="VOLATILITY_STOP_LIMIT_MISSING"' not in source, (
        "固定 incident_key 会让实盘与模拟共用一个告警事件，两边互相覆盖"
    )
    assert "VOLATILITY_STOP_LIMIT_MISSING:" in source
    assert "_campaign_tenant" in source
