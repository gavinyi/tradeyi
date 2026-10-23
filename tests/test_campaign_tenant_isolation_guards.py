"""P0-9 守卫（Chief 10:32 派）：跨租户隔离的**行为**层。

- ② 带租户键 + campaign **不在**清单 + 券商**仍有持仓** → **不删**
  （守 `volatility_orphans.py:331` 那处保护 —— 它必须走共享入口
  `tenant_of_broker`；裸 `getattr(broker, "tenant_id", "")` 恒空 →
  只产无租户形态 → 带租户键当场失去保护）
- ③ 两租户各自 ADOPT 同 code → **两个 campaign 都在、互不覆盖**
  （守 `volatility_orphans.py:403`；验收判据见
  `docs/volatility_campaigns_tenant_partition_design.md` §7.1 / §8.3）

两条都**必须能在修复前的代码上失败**（先红后绿）。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 变异测试缝：允许把 AST 守卫指向副本根（不碰工作树）
GUARD_ROOT = Path(os.environ.get("TRADEYI_CAMPAIGN_GUARD_ROOT") or ROOT)

from futu_quant.config import delete_campaign, upsert_campaign  # noqa: E402
from futu_quant.store import TradingStore  # noqa: E402
from futu_quant.volatility_orphans import (  # noqa: E402
    VolatilityOrphanPosition,
    adopt_volatility_orphan_position,
    protected_volatility_state_keys,
)

REAL = "futu_real_master"
PAPER = "futu_paper_sandbox"
REAL_ACCOUNT = "281756481476169109"
PAPER_ACCOUNT = "20170229"
CODE = "US.META261030P660000"
UNDERLYING = "US.META"
PREFIX = "volatility_long_gamma:"


class _Broker:
    """能反解出租户的 broker 替身（`account_id` 决定租户，与生产一致）。"""

    def __init__(self, account_id: str, positions: pd.DataFrame | None = None) -> None:
        self.account_id = account_id
        self.trading_env = "REAL"
        self._positions = positions if positions is not None else pd.DataFrame()

    def positions(self) -> pd.DataFrame:
        return self._positions

    def orders(self) -> pd.DataFrame:
        return pd.DataFrame()

    def is_cancellable_status(self, status) -> bool:
        return str(status).upper() in {"SUBMITTED", "WAITING_SUBMIT", "SUBMITTING"}


def _orphan_item(code: str = CODE) -> VolatilityOrphanPosition:
    return VolatilityOrphanPosition(
        code=code, underlying=UNDERLYING, quantity=1, can_sell_quantity=1,
        cost_price=10.0, nominal_price=7.4, market_value=740.0, pnl_value=-260.0,
        pnl_ratio=-0.26, strategy_net_quantity=1.0, pending_order_count=0,
        status="SELLABLE_ORPHAN", reason="守卫用例",
    )


def test_tenant_scoped_key_protected_while_broker_position_exists(tmp_path):
    """② 券商仍有持仓时，带租户的状态键必须被保护 —— 即使该 code 不在 campaign 清单里。

    修复前：`protected_volatility_state_keys` 用裸
    `getattr(broker, "tenant_id", "")` → 恒空 → 只产
    `volatility_long_gamma:{code}`，带租户形态
    `volatility_long_gamma:{tenant}:{code}` **不在保护集里** → 本用例转红。
    """
    store = TradingStore(tmp_path / "protect.db")
    tenant_key = f"{PREFIX}{REAL}:{CODE}"

    store.save_campaign_states({
        tenant_key: {"previous_position_quantity": 1.0},
        f"{PREFIX}{CODE}": {"previous_position_quantity": 1.0},
    })

    positions = pd.DataFrame([{"code": CODE, "qty": 1.0, "cost_price": 10.0}])
    protected = protected_volatility_state_keys(
        store, positions, pd.DataFrame(), _Broker(REAL_ACCOUNT),
    )

    assert tenant_key in protected, (
        f"P0-9 复现：券商仍有 {CODE} 持仓，但带租户的状态键 {tenant_key} "
        f"**没有被保护** —— 它会在本轮被 prune_campaign_states 当 stale 删掉。\n"
        f"  实际保护集：{sorted(protected)}"
    )

    # 端到端确认：把保护集交给真正的剪除入口，键必须活下来
    removed = store.prune_campaign_states(PREFIX, protected, set())
    assert tenant_key in store.campaign_states(), (
        f"剪除入口仍删掉了被保护的键 {tenant_key}（removed={removed}）"
    )


def test_legacy_and_tenant_scoped_campaign_for_same_code_collapse_to_one(tmp_path):
    """同一 code：一条带 `tenant_id`、一条不带 → 必须**只剩一条**。

    Chief 10:56 派。`_campaign_identity` 已带租户前缀（`config.py:384-386`），
    于是 `{tenant}|{code}` 与 `{code}` 是**两个不同身份** → 同 code 落**两条**。

    这正是 `_campaign_identity` 的 docstring 自己警告的形态（`config.py:368-372`）：
    「同一个合约可能因重复接管而带上不同的 campaign_id……worker 每轮把它当两个
    campaign 执行，决策重复且互相干扰」。

    **为什么在写入方补齐前就必须红**：四个写入方（雷达建/滚、`run_volatility_strategy`
    的 retained/rolled、UI）目前**都不传 `tenant_id`** → 雷达建的是无租户记录
    （身份 `{code}`），ADOPT 出来的是带租户记录（身份 `{tenant}|{code}`）→
    **同一个 code 两条记录**，正是 docstring 警告的那个 bug。
    """
    store = TradingStore(tmp_path / "collapse.db")

    # 形态一：无租户（雷达新建 / 未补 tenant_id 的写入方）
    upsert_campaign(
        "VOLATILITY_CAMPAIGNS",
        {"code": CODE, "underlying": UNDERLYING, "campaign_id": f"orphan:{CODE}"},
        store,
    )
    # 形态二：带租户（ADOPT 路径，已走共享入口）
    upsert_campaign(
        "VOLATILITY_CAMPAIGNS",
        {
            "code": CODE, "underlying": UNDERLYING, "tenant_id": REAL,
            "campaign_id": f"{REAL}|orphan:{CODE}",
        },
        store,
    )

    saved = store.get_runtime_config("VOLATILITY_CAMPAIGNS")
    campaigns = saved[0] if saved else []
    same_code = [c for c in campaigns if str(c.get("code")) == CODE]

    assert len(same_code) == 1, (
        f"P0-9 复现：同一个 code {CODE} 落库 **{len(same_code)}** 条 —— "
        "无租户形态与带租户形态成了两个身份，worker 会把它当两个 campaign 执行。\n"
        f"  实际记录：{[(c.get('tenant_id'), c.get('campaign_id')) for c in same_code]}\n"
        "  根因：`upsert_campaign` 按 `_campaign_identity`（租户+code）去重，"
        "而无租户记录与带租户记录的身份不同 → 不去重。\n"
        "  修法方向：带租户的 upsert 必须同时清掉同 code 的**无租户 legacy 记录**"
        "（或写入方全部补 tenant_id，使两种形态不再并存）。"
    )


def _dict_keys(node: ast.Dict) -> set[str]:
    keys = set()
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
    return keys


def _resolve_campaign_dict(func_node: ast.AST, arg: ast.expr) -> ast.Dict | None:
    """把 `upsert_campaign(...)` 的 campaign 实参解析成 dict 字面量。

    支持三种形态：直接 dict、名字（找同函数内的赋值）、`X[-1]`（找 `X.append(dict)`）。
    解析不出来返回 None —— 调用方必须把它当成**未显式传 tenant_id** 处理。
    """
    if isinstance(arg, ast.Dict):
        return arg
    if isinstance(arg, ast.Name):
        for node in ast.walk(func_node):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == arg.id for t in node.targets
            ) and isinstance(node.value, ast.Dict):
                return node.value
        return None
    if isinstance(arg, ast.Subscript) and isinstance(arg.value, ast.Name):
        for node in ast.walk(func_node):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == arg.value.id
                and node.args
                and isinstance(node.args[0], ast.Dict)
            ):
                return node.args[0]
    return None


def test_delete_campaign_is_fail_closed_on_ambiguous_code(tmp_path):
    """`delete_campaign` 模糊删除必须**响亮拒绝**，不得静默删掉别人。

    `config.py:440-492`：身份改成带租户前缀后，调用方仍传裸 code。
    若"裸 code 删所有租户副本"，控制页（租户盲的清理按钮）会删掉**另一个租户**
    的 campaign → 那个租户的持仓失去管理（10-22 META 的失效模式）。

    判据：裸 code 命中**多个租户** → 抛异常；命中一个 → 删；完整身份 → 精确删一条。
    """
    store = TradingStore(tmp_path / "delete.db")
    real = {"code": CODE, "underlying": UNDERLYING, "tenant_id": REAL,
            "campaign_id": f"{REAL}|orphan:{CODE}"}
    paper = {"code": CODE, "underlying": UNDERLYING, "tenant_id": PAPER,
             "campaign_id": f"{PAPER}|orphan:{CODE}"}

    upsert_campaign("VOLATILITY_CAMPAIGNS", real, store)
    upsert_campaign("VOLATILITY_CAMPAIGNS", paper, store)

    # ① 完整身份 → 精确删掉实盘那条，模拟那条必须留下
    delete_campaign("VOLATILITY_CAMPAIGNS", f"{REAL}|{CODE}", store)
    remaining = [c for c in (store.get_runtime_config("VOLATILITY_CAMPAIGNS") or [[]])[0]
                 if str(c.get("code")) == CODE]
    assert len(remaining) == 1 and remaining[0].get("tenant_id") == PAPER, (
        f"完整身份删除未精确命中：剩 {[(c.get('tenant_id')) for c in remaining]}"
    )

    # ② 裸 code 命中两个租户 → 必须抛异常（不得静默删掉别人）
    upsert_campaign("VOLATILITY_CAMPAIGNS", real, store)
    with pytest.raises(ValueError, match="拒绝模糊删除"):
        delete_campaign("VOLATILITY_CAMPAIGNS", CODE, store)
    after = [c for c in (store.get_runtime_config("VOLATILITY_CAMPAIGNS") or [[]])[0]
              if str(c.get("code")) == CODE]
    assert len(after) == 2, f"模糊删除竟然删掉了记录：剩 {len(after)} 条"

    # ③ 裸 code 只命中一个租户 → 兼容旧调用方，正常删
    delete_campaign("VOLATILITY_CAMPAIGNS", f"{REAL}|{CODE}", store)   # 先删掉 real，剩 paper
    delete_campaign("VOLATILITY_CAMPAIGNS", CODE, store)               # 裸 code 命中 1 个租户 → 正常删
    after2 = [c for c in (store.get_runtime_config("VOLATILITY_CAMPAIGNS") or [[]])[0]
               if str(c.get("code")) == CODE]
    assert len(after2) == 0, f"兼容删除失败：剩 {len(after2)} 条"


def test_campaign_writers_pass_tenant_id():
    """五个 campaign 写入方必须**显式传 `tenant_id`**（否则身份退化成全局形态）。

    Chief 10:56 派。`_campaign_identity` 已带租户前缀（`config.py:384-386`），
    但**写入方不传 `tenant_id`** 时，身份仍是裸 `{code}` —— 与 ADOPT 产出的
    `{tenant}|{code}` 是**两个身份**，同一个 code 落两条记录。

    判据：每个 `upsert_campaign(...)` 调用点的 campaign 实参（dict / 名字 / `X[-1]`）
    解析出的 dict **必须含 `tenant_id` 键**。
    """
    targets = [
        GUARD_ROOT / "src" / "futu_quant" / "volatility_scanner.py",
        GUARD_ROOT / "scripts" / "run_volatility_strategy.py",
        GUARD_ROOT / "pages" / "10_volatility_options_strategy.py",
    ]
    offenders: list[str] = []
    checked = 0

    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # 作用域 = 模块本身 + 所有函数（pages 的写入点在模块层，不在函数里）
        scopes = [tree] + [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        seen: set[int] = set()
        for scope in scopes:
            for node in ast.walk(scope):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "upsert_campaign"):
                    continue
                if node.lineno in seen:
                    continue
                seen.add(node.lineno)
                checked += 1
                arg = node.args[1] if len(node.args) > 1 else None
                if arg is None:
                    offenders.append(f"{path.name}:{node.lineno} 未传 campaign 实参")
                    continue
                campaign = _resolve_campaign_dict(scope, arg)
                if campaign is None:
                    offenders.append(
                        f"{path.name}:{node.lineno} campaign 实参 `{ast.unparse(arg)}` "
                        "解析不出 dict —— 请**显式**传 `\"tenant_id\": tenant_id_of_broker(broker) or \"\"`"
                    )
                elif "tenant_id" not in _dict_keys(campaign):
                    offenders.append(
                        f"{path.name}:{node.lineno} campaign dict 缺 `tenant_id` 键"
                    )

    assert checked >= 5, f"应至少扫到 5 个 upsert_campaign 写入点，实际 {checked}"
    assert not offenders, (
        "以下 campaign 写入方未显式传 `tenant_id` —— 它们产出的记录身份是裸 `{code}`，"
        "会与 ADOPT 产出的 `{tenant}|{code}` 同 code 共存两条：\n  "
        + "\n  ".join(offenders)
    )


def test_two_tenants_adopting_same_code_do_not_overwrite(tmp_path):
    """③ 两租户各自 ADOPT 同一个 code → 两个 campaign 都在、互不覆盖。

    判据来源：`docs/volatility_campaigns_tenant_partition_design.md` §7.1
    「identity 必须带租户前缀……两租户同一 code 会互相顶掉」+ §8.3 验收第 3 条。

    修复前：`adopt_volatility_orphan_position` 写**全局键** `VOLATILITY_CAMPAIGNS`，
    而 `config._campaign_identity` 只按 `code` 去重 → 后者顶掉前者 → 本用例转红。

    **#1 键级分区之后**：ADOPT 写的是**租户键**（`VOLATILITY_CAMPAIGNS:{tenant}`），
    全局键里本来就该是空的。所以断言同时覆盖两件事：
    ① 两个租户键各自有那一条；② 全局键**没有**被写入（写了就说明分区没落地）。
    """
    store = TradingStore(tmp_path / "adopt.db")

    adopt_volatility_orphan_position(store, _orphan_item(), [], REAL)
    adopt_volatility_orphan_position(store, _orphan_item(), [], PAPER)

    def _rows(key: str) -> list[dict]:
        saved = store.get_runtime_config(key)
        value = saved[0] if saved else []
        return [c for c in value if str(c.get("code")) == CODE] if isinstance(value, list) else []

    real_rows = _rows(f"VOLATILITY_CAMPAIGNS:{REAL}")
    paper_rows = _rows(f"VOLATILITY_CAMPAIGNS:{PAPER}")
    legacy_rows = _rows("VOLATILITY_CAMPAIGNS")

    assert len(real_rows) == 1 and len(paper_rows) == 1, (
        f"两租户应各自持有一条 {CODE} —— 实盘键 {len(real_rows)} 条、模拟键 {len(paper_rows)} 条。\n"
        "  若都是 0 且全局键里有 2 条，说明写的仍是全局键（键级分区没落地）。"
    )
    assert not legacy_rows, (
        f"全局键 VOLATILITY_CAMPAIGNS 里出现了 {len(legacy_rows)} 条 —— "
        "键级分区后 ADOPT 必须写租户键，写全局键就是跨租户共用一份清单。"
    )
    tenants = sorted(
        str(c.get("tenant_id") or "")
        for c in (*real_rows, *paper_rows)
    )
    assert tenants == sorted([REAL, PAPER]), f"两个租户的归属都应在，实际：{tenants}"
