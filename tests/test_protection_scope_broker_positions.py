"""持仓保护的范围：必须看得到**券商侧**持仓，而不只是策略账本。

2026-10-30 现场：OpenD 断线 28 分钟，保护事件写「检测到未平期权 2 张」，
而那 2 张全部是 `sources: ["virtual"]` 的虚拟盘仓位；与此同时券商实盘持有
`US.NVDA` 6 股 + `US.GLD` 2.4344 股（约 $2,175），事件里一个字都没提。
用户读到的「有仓被保护」和真实的「有仓没人管」是两件事。

本文件锁定三件事：
1. 券商快照里的期权要并入保护列表（与策略账本去重合并）；
2. 券商正股要单独计数，不能被折进 `open_option_count` 混为一谈；
3. 正股**不触发升级** —— 长期底仓会让每次 OpenD 抖动都升级成「策略中断」。

变异可红：删掉 `_broker_positions_split` 里对 `broker_snapshot` 的读取，
前两条转红；把 `escalatable = bool(options)` 改成 `bool(options or stocks)`，
第三条转红。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from futu_quant.connectivity_protection import (
    active_protected_option_positions,
    active_protected_stock_positions,
    protected_position_summary,
)
from futu_quant.store import TradingStore

NEW_YORK = ZoneInfo("America/New_York")
RUN_SERVICE = Path(__file__).resolve().parents[1] / "scripts" / "run_service.py"
SCANNER = Path(__file__).resolve().parents[1] / "src" / "futu_quant" / "volatility_scanner.py"


def _contract_code(days_from_now: int) -> str:
    day = datetime.now(NEW_YORK).date() + timedelta(days=days_from_now)
    return f"US.QQQ{day.strftime('%y%m%d')}C735000"


@pytest.fixture()
def store(tmp_path):
    return TradingStore(tmp_path / "protection-scope.db")


def _seed_broker(store, rows, key="positions_real"):
    store.save_broker_snapshot(key, rows)


# --------------------------------------------------------------- 券商期权并入保护


def test_broker_option_position_is_included(store):
    """券商快照里的未平期权必须进入保护列表 —— 这是原先最大的盲区。"""
    live = _contract_code(3)
    _seed_broker(store, [
        {"code": live, "qty": 1.0, "market_val": 420.0, "pl_val": -55.0},
    ])

    rows = active_protected_option_positions(store)

    assert [row["symbol"] for row in rows] == [live]
    assert rows[0]["sources"] == ["broker"], "券商来源未标注，事后无法判断是实盘还是账本"


def test_broker_and_ledger_options_merge_without_double_counting(store):
    """同一张合约同时出现在券商快照和虚拟账本时，要合并且只算一次来源。"""
    live = _contract_code(4)
    store.set_virtual_allocation("volatility_long_gamma", live, 1.0, 1.0)
    _seed_broker(store, [{"code": live, "qty": 2.0}])

    rows = active_protected_option_positions(store)

    assert len(rows) == 1
    assert rows[0]["quantity"] == pytest.approx(3.0), "合并后数量应为两路之和"
    assert rows[0]["sources"] == ["broker", "virtual"]


def test_expired_broker_option_is_ignored(store):
    """券商侧的过期合约同样要跳过（2026-10-27 那类残留会反复触发保护）。"""
    live = _contract_code(5)
    _seed_broker(store, [
        {"code": _contract_code(-80), "qty": 1.0},
        {"code": live, "qty": 1.0},
    ])

    assert [row["symbol"] for row in active_protected_option_positions(store)] == [live]


# ------------------------------------------------------------------- 正股单独计数


def test_broker_stock_positions_are_counted_separately(store):
    """正股不能混进 `open_option_count` —— 两者风险量级完全不同。"""
    live = _contract_code(6)
    _seed_broker(store, [
        {"code": live, "qty": 1.0},
        {"code": "US.NVDA", "qty": 6.0, "market_val": 1279.8, "pl_val": 1381.97, "pl_ratio": 0.9559},
        {"code": "US.GLD", "qty": 2.4344, "market_val": 894.98, "pl_val": -172.48, "pl_ratio": -0.1616},
    ])

    summary = protected_position_summary(store)

    assert summary["option_count"] == 1
    assert summary["stock_count"] == 2, "两只正股没被统计 —— 用户会以为账户空仓"
    assert [row["symbol"] for row in summary["stocks"]] == ["US.GLD", "US.NVDA"]
    gld = next(row for row in summary["stocks"] if row["symbol"] == "US.GLD")
    # 富途的 pl_ratio 是小数（-0.1616 = -16.16%），页面按比例展示
    assert gld["pl_ratio"] == pytest.approx(-0.1616, abs=0.0001), "浮亏比例没带出来，看不出风险量级"


def test_zero_quantity_rows_are_skipped(store):
    """已平仓行（qty=0）不能算持仓 —— 富途快照会保留这类残行。"""
    _seed_broker(store, [
        {"code": "US.GOOG261106P290000", "qty": 0.0, "pl_val": 67.0},
        {"code": "US.NVDA", "qty": 0.0},
    ])

    summary = protected_position_summary(store)

    assert summary["option_count"] == 0
    assert summary["stock_count"] == 0


def test_stock_only_does_not_escalate_protection(store):
    """只有正股时不升级 —— 否则长期底仓会让每次抖动都重启服务。

    升级判据在 `activate_protective_recovery` 里是 `escalatable = bool(options)`。
    这里是源码级断言：这类「不要做什么」的约束没有便宜的行为测试入口
    （要构造盘中 + 失败 3 次 + 多租户三条链路），但一旦被改宽，代价是每次
    抖动都暂停开新仓。
    """
    src = RUN_SERVICE.read_text(encoding="utf-8")
    assert "escalatable = bool(options)" in src, (
        "升级判据被改成包含正股 —— 长期底仓会让每次 OpenD 抖动都升级为策略中断"
    )
    assert "if failures < threshold or not escalatable:" in src, (
        "非升级路径丢了，只有正股时仍会走到重启分支"
    )
    # 只有正股时永远不会升级，若不节流就是每 30 秒一条重复事件
    assert "if not escalatable and failures > 1:" in src, (
        "缺节流：纯正股持仓下 28 分钟的断线会刷出 50+ 条相同事件"
    )


# ----------------------------------------------------------------- 防御与口径


def test_missing_broker_snapshot_capability_does_not_crash():
    """轻量 fake store 没有 `broker_snapshot` 时必须静默跳过券商来源。"""

    class _LedgerOnly:
        def virtual_positions(self):
            return [{"symbol": _contract_code(7), "quantity": 1.0, "strategy_id": "volatility_long_gamma"}]

        def shadow_open_positions(self):
            return []

    rows = active_protected_option_positions(_LedgerOnly())

    assert [row["symbol"] for row in rows] == [_contract_code(7)]


def test_simulate_env_reads_sim_snapshot(store):
    """模拟租户不能读到实盘快照 —— 否则会把别人的仓算成自己的保护对象。"""
    real_only = _contract_code(8)
    sim_only = _contract_code(9)
    _seed_broker(store, [{"code": real_only, "qty": 1.0}], key="positions_real")
    _seed_broker(store, [{"code": sim_only, "qty": 1.0}], key="positions_sim")

    assert [r["symbol"] for r in active_protected_option_positions(store, trading_env="REAL")] == [real_only]
    assert [r["symbol"] for r in active_protected_option_positions(store, trading_env="SIMULATE")] == [sim_only]
    assert active_protected_stock_positions(store, trading_env="SIMULATE") == []


# ------------------------------------------------------- 候选被过滤 ≠ 信号链断裂


def test_candidate_filtered_is_warning_not_error():
    """入场过滤器拒单是设计内行为，不能标 ERROR、不能叫 BROKEN。

    2026-10-29 恢复后 AMZN 被 IV 百分位 94% 拦下，却推了一条 ERROR
    「VOLATILITY_SIGNAL_CHAIN_BROKEN」，与真正的决策链静默断裂同名同级。
    """
    src = SCANNER.read_text(encoding="utf-8")
    assert '"WARNING", "VOLATILITY_CANDIDATE_FILTERED"' in src, "候选被过滤仍标成 ERROR 或旧名"
    # 真断链那一处必须保留 ERROR + BROKEN 语义
    assert src.count('"ERROR", "VOLATILITY_SIGNAL_CHAIN_BROKEN"') == 1, (
        "真断链的告警被一起改掉了 —— 那才是必须排查的故障"
    )
