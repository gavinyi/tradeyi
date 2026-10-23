"""P0-3 端到端守卫：硬止损命中后，**最终提交的退出单必须存在**。

背景（10-22 实盘事故，$366 超调）：
  `run_campaign` 的 VOLATILITY 分支里，硬止损原本只在 EARNINGS 分支被守护；
  VOLATILITY 分支的分批止盈 / 常规决策会**无条件覆盖** `action="SELL"`，
  于是亏损已过线却返回 HOLD，硬止损形同虚设。

本文件**不读源码、不断言字符串**（那是空转守卫）——它真的跑 `run_campaign`，
构造「新开仓 + 时间衰减未生效 + 亏损超 `max_position_loss_ratio`」的 VOLATILITY
campaign，然后断言**退出单真的落进了执行通道**。
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from futu_quant.store import TradingStore  # noqa: E402


def _load_gamma():
    """载入 run_gamma_strategy；变异测试可通过环境变量指定副本路径。

    这是**刻意的测试缝**：变异测试需要确保跑的是被变异的副本，而不是工作树模块。
    """
    override = os.environ.get("TRADEYI_GAMMA_MODULE")
    if override:
        spec = importlib.util.spec_from_file_location("run_gamma_strategy", override)
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_gamma_strategy"] = module
        spec.loader.exec_module(module)
        return module
    import run_gamma_strategy as module
    return module

CODE = "US.META261030P660000"
UNDERLYING = "US.META"
COST = 10.0          # 持仓成本
BID = 7.4            # 现价 → pnl_ratio = -0.26：刚过 max_position_loss_ratio(0.25) 的线


class _LossBroker:
    """一个「持仓已腰斩」的期权账户。"""

    account_id = "281756481476169109"
    trading_env = "REAL"

    def __init__(self, bid: float) -> None:
        self._bid = bid

    def positions(self) -> pd.DataFrame:
        return pd.DataFrame([{"code": CODE, "qty": 1.0, "cost_price": COST}])

    def orders(self) -> pd.DataFrame:
        return pd.DataFrame()

    def candles(self, symbol, count: int = 90, **kwargs) -> pd.DataFrame:
        # 硬止损判定不依赖 K 线内容，但波动率阈值画像要读 close 列
        bars = max(int(count), 10)
        return pd.DataFrame({
            "close": [COST * 0.9] * bars,
            "high": [COST * 0.95] * bars,
            "low": [COST * 0.85] * bars,
            "volume": [1_000_000] * bars,
        })

    def snapshots(self, symbols):
        # 行情时间戳必须**相对当前时间**生成：写死成 "2026-10-23 09:30:00"（NY）
        # 时，美股开盘 15 分钟后 quote 就超过 900s 新鲜度上限，本用例会随墙钟
        # 由绿转红（10-23 22:02 实测失败，与业务代码无关）。
        from datetime import datetime as _dt, timedelta as _td
        from zoneinfo import ZoneInfo as _TZ

        stamp = (_dt.now(_TZ("America/New_York")) - _td(seconds=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return pd.DataFrame([{
            "code": s, "bid_price": self._bid, "ask_price": self._bid + 0.02,
            "last_price": self._bid, "update_time": stamp,
            "option_gamma": 0.01, "option_theta": 0.05, "option_implied_volatility": 0.30,
            "option_contract_multiplier": 100,
        } for s in symbols])

    def is_cancellable_status(self, status) -> bool:
        return str(status).upper() in {"SUBMITTED", "WAITING_SUBMIT", "SUBMITTING"}


def _campaign() -> dict:
    return {
        "code": CODE, "underlying": UNDERLYING, "trigger_mode": "VOLATILITY",
        "enabled": True, "exit_only": False,
        "option_type": "PUT", "structure": "LONG_OPTION",
        "optimizer_version": 2,
        # 关键：两个阈值**故意不同**，用来隔离 P0-3 的守护。
        #   硬止损块用 max_position_loss_ratio=0.25（本场景 -26% 命中 → SELL）
        #   常规决策 decide_volatility_action 用 stop_loss=0.60（-26% 未命中 → HOLD）
        # 于是：有守护 → SELL 提交；无守护 → 被常规决策覆盖成 HOLD。
        # 注意：`stop_loss` 只能出现一次（重复键会被后者覆盖，隔离就失效）。
        "max_position_loss_ratio": 0.25, "max_position_loss_usd": 300.0,
        "stop_loss": 0.60,
        "legs": [{"code": CODE, "side": "BUY", "ratio": 1, "role": "PRIMARY"}],
        "entry_hours": 6, "max_hold_hours": 24,
        "contracts_per_stage": 1, "max_stages": 1, "max_premium_per_stage": 3000.0,
        "profit_target": 0.50, "trailing_stop": 0.25,
        "entry_zscore": 2.0, "minimum_underlying_move": 0.02,
        "entry_confirmation_minutes": 5,
        "entry_confirmation_min_volume_ratio": 0.80,
        "entry_confirmation_min_volume_retention": 0.65,
        "entry_confirmation_require_active": True,
        "cooldown_hours": 24, "cooldown_bypass_enabled": True,
        "event_date": "2026-10-23", "campaign_id": "cmp-1",
    }


class _RecordingExecutor:
    """只实现 run_campaign 用到的执行器接口，并记录提交。"""

    def __init__(self, store: TradingStore) -> None:
        self.store = store
        self.submitted: list[dict] = []

    def submit_limit(self, strategy_id, intent_key, symbol, side, quantity, price,
                     metadata=None, **kwargs):
        self.submitted.append({
            "strategy_id": strategy_id, "symbol": symbol, "side": side,
            "quantity": quantity, "price": price, "metadata": metadata or {},
        })
        return type("R", (), {"status": "SUBMITTED", "broker_order_id": "BRK-X",
                              "message": ""})()

    def alerts(self):
        return self

    def send(self, *args, **kwargs):
        return None


def test_hard_stop_submits_exit_order_end_to_end(tmp_path, monkeypatch):
    """端到端：亏损超线的 VOLATILITY campaign，最终必须真的提交一张退出单。"""
    rg = _load_gamma()

    store = TradingStore(tmp_path / "hs.db")
    executor = _RecordingExecutor(store)

    # 时间衰减未生效（刚开仓），让硬止损成为唯一触发条件
    monkeypatch.setattr(rg, "volatility_signal_state", lambda *a, **k: {})
    monkeypatch.setattr(rg, "sync_volatility_position_cycle", lambda *a, **k: None)
    # 绕过"市场未开盘"门禁 —— 本用例只关心硬止损守护，不关心交易时段
    monkeypatch.setattr(
        rg.MARKET_CALENDAR, "session",
        lambda _now: type("S", (), {"regular_open": staticmethod(lambda _n: True)})(),
    )

    rg.run_campaign(
        _LossBroker(bid=BID), executor, _campaign(), {},
        strategy_id="volatility_long_gamma",
    )

    sells = [c for c in executor.submitted if str(c["side"]).upper() == "SELL"]
    assert sells, (
        "P0-3 复现：亏损 -26%（超 -25% 硬止损线）却**没有提交任何退出单** —— "
        f"硬止损被常规决策覆盖成 HOLD。实际提交：{executor.submitted}"
    )
    assert sells[0]["symbol"] == CODE, "退出单标的错误"
    # 必须来自**硬止损块**（:741-748 的 `volatility_hard_loss_exit_reason`），
    # 而不是常规决策的另一个退出理由 —— 否则这条守卫在"常规路径也 SELL"时
    # 会空转通过（变异测试已证：单层去掉时两条路径都提交 SELL）。
    reason = str(sells[0]["metadata"].get("reason") or "")
    assert "单笔硬止损触发" in reason, (
        f"退出单的理由不是硬止损块（实际：{reason!r}）—— "
        "常规决策路径的理由是「触发波动率策略硬止损」，两者都含「硬止损」但前缀不同；"
        "断言宽了会空转，必须钉住硬止损块的独特前缀"
    )


def _gamma_source_path() -> Path:
    """结构守卫读的源码路径；变异测试可通过同一个环境变量指向副本。"""
    override = os.environ.get("TRADEYI_GAMMA_MODULE")
    if override:
        return Path(override)
    return ROOT / "scripts" / "run_gamma_strategy.py"


def _action_value_node(node: ast.Assign):
    """从赋值语句里取出赋给 `action` 的那个值节点（支持元组解包）。"""
    for target in node.targets:
        if isinstance(target, ast.Name) and target.id == "action":
            return node.value
        if isinstance(target, ast.Tuple):
            for index, elt in enumerate(target.elts):
                if isinstance(elt, ast.Name) and elt.id == "action":
                    value = node.value
                    if isinstance(value, ast.Tuple) and index < len(value.elts):
                        return value.elts[index]
    return None


def test_no_branch_downgrades_sell_after_hard_loss_layer():
    """结构守卫（Chief 四条之③）：`:1169` 之后不得有分支把 action 从 SELL 降级。

    这是**纵深防御第 2 层**（分支之后的重算）的防回潮锁：第 1 层（`:1048`）挡
    分支内的提前覆盖，第 2 层（`:1169-1172`）挡分支之后的重算与降级。两层各自
    独立，删任一层行为不变、删两层失效（变异 C 已证）。

    用 **AST 判定**，不是字符串匹配 —— 字符串会被注释里的同样字样误报。
    判据可证伪：把 `:1183` 的 `action, reason = "SELL", ...` 改成
    `action, reason = "HOLD", ...`，这条守卫必须转红。
    """
    tree = ast.parse(_gamma_source_path().read_text(encoding="utf-8"))

    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_campaign"
    )
    anchor = next(
        node for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name) and node.test.id == "hard_loss_reason"
    )

    offenders: list[str] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or node.lineno <= anchor.lineno:
            continue
        value = _action_value_node(node)
        if value is None:
            continue
        if not (isinstance(value, ast.Constant) and value.value == "SELL"):
            offenders.append(f"L{node.lineno}: action = {ast.unparse(value)}")

    assert not offenders, (
        "硬止损第 2 层之后仍存在把 action 从 SELL 降级的分支 —— "
        "纵深防御第 2 层被架空：" + "；".join(offenders)
    )


def test_two_hard_stop_layers_read_identical_limits():
    """数据层守卫（Reviewer 保留意见）：两层必须读**同一份**解析结果。

    两层在**控制流**上独立（第 1 层 `:741` 挡分支内覆盖，第 2 层 `:1163` 挡
    分支后降级），但在**数据**上耦合 —— 都读 `max_position_loss_usd` /
    `max_position_loss_ratio`。若有一层被改成读另一个键、或另一个回落默认值，
    两层就会**静默分歧**（同一件事两个数，历史上 100/300 就是这样打架的）。

    AST 判定：两处 `volatility_hard_loss_exit_reason(...)` 的实参必须**结构完全相同**。
    判据可证伪：把第 1 层的回落值 `300` 改成 `100`，这条守卫必须转红。
    """
    tree = ast.parse(_gamma_source_path().read_text(encoding="utf-8"))
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_campaign"
    )
    calls = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "volatility_hard_loss_exit_reason"
    ]
    assert len(calls) == 2, (
        f"硬止损应恰好有 2 处调用（第 1 层 :741 / 第 2 层 :1163），实际 {len(calls)} 处"
    )
    assert ast.dump(calls[0]) == ast.dump(calls[1]), (
        "两层硬止损读到的是**不同的实参** —— 同一不变量两份解析结果会静默分歧：\n"
        f"  第 1 层：{ast.unparse(calls[0])}\n"
        f"  第 2 层：{ast.unparse(calls[1])}"
    )


def test_normal_volatility_decision_is_not_affected(tmp_path, monkeypatch):
    """反向守卫：未触发硬止损时，这次改动不得影响常规 VOLATILITY 决策。

    现价高于成本（盈利），硬止损不应触发；此路径的行为必须与改动前一致
    （不因 P0-3 的守护而把所有决策都变成 SELL）。
    """
    rg = _load_gamma()

    store = TradingStore(tmp_path / "normal.db")
    executor = _RecordingExecutor(store)

    monkeypatch.setattr(rg, "volatility_signal_state", lambda *a, **k: {})
    monkeypatch.setattr(rg, "sync_volatility_position_cycle", lambda *a, **k: None)
    monkeypatch.setattr(
        rg.MARKET_CALENDAR, "session",
        lambda _now: type("S", (), {"regular_open": staticmethod(lambda _n: True)})(),
    )

    rg.run_campaign(
        _LossBroker(bid=COST * 1.5), executor, _campaign(), {},
        strategy_id="volatility_long_gamma",
    )

    sells = [c for c in executor.submitted if str(c["side"]).upper() == "SELL"]
    assert not sells, (
        f"盈利仓位被误提交退出单 —— P0-3 守护影响到了常规决策：{executor.submitted}"
    )
