"""#1 键级分区 · 状态键半边：**四个读写点必须用同一个拼法**。

背景（Chief 10-23 在迁移 dry-run 里抓到，本会话最重要的阻断项）：

迁移 A 之后 campaign 清单搬到租户键，而状态键有**三种互不兼容的格式**：

* ``order_push``      ``volatility_long_gamma:{tenant}:{code}``（带租户）
* ``volatility_orphans`` ``volatility_long_gamma:{code}``（不带）
* ``run_gamma_strategy`` ``{strategy_id}:{code}``（不带）
* ``run_service``     按租户读

后果：``volatility_orphans`` / ``run_gamma_strategy`` 写的状态**读侧看不到**，
存量全局状态键全部变孤儿 —— ``peak`` / ``position_opened_at`` / ``entry_iv_actual``
这些**止盈止损基准当场丢失**。同一个 code 写两种键 = 同一份状态裂成两半，
比不分区更糟（违反「写侧必须是读侧的超集」）。

修复：新增 ``volatility_orphans.volatility_write_state_key()`` 作为**写形态的唯一出处**，
四个点都调它；读侧保留"迁移前形态 → 写形态"的**就地收敛**（只搬不复制）。

本文件守两件事：
1. **没有第二份拼法** —— 全仓不得再出现手拼的 ``volatility_long_gamma:{tenant}:...``；
2. **四个点确实调了共享助手** —— 用手拼的写法改回去，本文件立刻转红。
"""
from __future__ import annotations

import ast
from pathlib import Path

from futu_quant.tenant import tenant_id_of_broker

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "futu_quant"
SCRIPTS = ROOT / "scripts"

WRITE_SITES = {
    "order_push.py": SRC / "order_push.py",
    "volatility_orphans.py": SRC / "volatility_orphans.py",
    "run_gamma_strategy.py": SCRIPTS / "run_gamma_strategy.py",
    "run_service.py": SCRIPTS / "run_service.py",
}
HAND_BUILT_PREFIX = "volatility_long_gamma:"


def _sources() -> list[tuple[str, str, ast.Module]]:
    out = []
    for base in (SRC, SCRIPTS):
        for path in sorted(base.rglob("*.py")):
            if "mutsrc" in str(path):
                continue
            text = path.read_text(encoding="utf-8")
            out.append((str(path.relative_to(ROOT)), text, ast.parse(text)))
    return out


def _fstring_prefixes(tree: ast.Module) -> list[str]:
    """收集模块里所有 f-string 的**字面量前缀**（用于识别手拼的状态键）。"""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        head = ""
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                head += value.value
            else:
                break
        if head:
            found.append(head)
    return found


def test_only_the_helper_builds_tenant_scoped_state_keys():
    """手拼的带租户状态键**只能**出现在共享助手里。

    判据不是"有没有 `tenant_id` 字样"（那是形态匹配，会被变量名漂移骗过），
    而是**字面量前缀**：`volatility_long_gamma:` 后面直接跟 `{` 的 f-string，
    在助手之外出现即说明有人手拼了状态键。
    """
    offenders: list[str] = []
    for rel, _text, tree in _sources():
        for prefix in _fstring_prefixes(tree):
            if not prefix.startswith(HAND_BUILT_PREFIX):
                continue
            # 助手里的两处是**定义**（无租户形态 + 带租户形态），允许存在
            if rel.endswith("volatility_orphans.py"):
                continue
            offenders.append(f"{rel}: f\"{prefix}...\"")
    assert not offenders, (
        "以下位置手拼了 volatility 状态键 —— 状态键的拼法必须只有 "
        "`volatility_write_state_key()` 一个出处，否则同一个 code 会写出两种键、"
        "读侧各看各的（peak / entry_iv_actual 丢失 → 止盈止损基准错）：\n  "
        + "\n  ".join(offenders)
    )


def test_all_four_write_sites_call_the_shared_helper():
    """四个读写点必须调用共享助手，且**至少有一次**是带租户的写形态。

    允许"不带租户"的调用存在（那是迁移前形态，用于读侧回落与无租户上下文），
    但**每个模块都必须存在带租户的那次调用** —— 否则该点仍在写旧格式。
    """
    problems: list[str] = []
    for name, path in WRITE_SITES.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "volatility_write_state_key"
        ]
        if not calls:
            problems.append(f"{name}: 没有调用 volatility_write_state_key()")
            continue
        tenant_calls = []
        for call in calls:
            args = [ast.unparse(a) for a in call.args]
            if len(args) < 2:
                continue
            if args[1] in {'""', "''"}:
                problems.append(f"{name}: tenant_id 传了空串常量 —— {args}")
                continue
            tenant_calls.append(args)
        if not tenant_calls:
            problems.append(
                f"{name}: 只有不带租户的调用 —— 该点仍在写迁移前的旧格式"
            )
    assert not problems, "状态键拼法未统一：\n  " + "\n  ".join(problems)


def test_written_key_is_readable_and_legacy_converges(tmp_path):
    """行为判据：`order_push` 写的键，`run_gamma_strategy` **必须能读到**。

    前两条是静态判据（有没有第二份拼法、四个点调没调助手）；这一条是**行为**判据 ——
    用两个真实调用点各自的写法各取一次键，断言它们**是同一个键**，
    再断言该键在库里真的读得出来。静态判据防"拼法分叉"，这条防"两边各自正确但键不同"。
    """
    from futu_quant.store import TradingStore
    from futu_quant.volatility_orphans import volatility_write_state_key

    tenant = "futu_paper_sandbox"
    code = "US.META261030P660000"

    # ① order_push 的写法（位置参数：code, tenant_id）
    written = volatility_write_state_key(code, tenant)
    # ② run_gamma_strategy 的写法（keyword：code, tenant_id, prefix=...）
    read_as_gamma = volatility_write_state_key(
        code, tenant_id_of_broker(_paper_broker()), prefix="volatility_long_gamma:",
    )
    assert read_as_gamma == written, (
        f"两个调用点取出的键不同：order_push={written!r} / "
        f"run_gamma_strategy={read_as_gamma!r} —— 写读不一致，状态会裂成两份"
    )
    assert written == f"volatility_long_gamma:{tenant}:{code}"

    store = TradingStore(tmp_path / "state_key.db")
    store.save_campaign_states({written: {"peak": 123.0, "entry_iv_actual": 0.4}})
    saved = store.campaign_states()
    assert written in saved, "写进去的键读不出来 —— 读侧拼法与写侧不一致"
    assert float(saved[written]["peak"]) == 123.0


def _paper_broker():
    """最小 broker：只需能被 `tenant_of_broker` 反解成模拟租户。"""

    class _B:
        account_id = "20170229"
        trading_env = "SIMULATE"

    return _B()


def test_helper_is_the_single_source_of_truth():
    """共享助手本身：无租户 → 旧形态；有租户 → 带租户形态。"""
    import importlib

    mod = importlib.import_module("futu_quant.volatility_orphans")
    helper = mod.volatility_write_state_key
    assert helper("US.X", "") == "volatility_long_gamma:US.X"
    assert helper("US.X", "futu_paper_sandbox") == (
        "volatility_long_gamma:futu_paper_sandbox:US.X"
    )
    # 前缀可配置（run_gamma_strategy 用 `{strategy_id}:`）
    assert helper("US.X", "t", prefix="volatility_long_gamma:") == (
        "volatility_long_gamma:t:US.X"
    )
    assert helper("US.X", "", prefix="volatility_long_gamma:") == "volatility_long_gamma:US.X"
