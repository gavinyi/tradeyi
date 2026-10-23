"""P0-9 守类守卫：**state key 里的租户成分，不得来自 broker 的读取**。

**判据（守数据来源，不守写法）**

> 任何被拼进 state key 的租户成分，如果它的值来自对 **broker** 的读取
> （``getattr(<broker>, "tenant_id")`` 或 ``<broker>.tenant_id``），守卫转红。

**为什么是这一条，而不是"必须来自 tenant_of_broker"**

Chief 最初提的是宽判据："``tenant_id`` 的赋值必须来自 ``tenant_of_broker`` /
``tenant_id_of_broker``"。**实测（``data/qa/_probe_key_source.py``）证明宽判据会误报
6 处合法来源**，它们都不是"从 broker 读"，因此不该被拦：

| 合法来源 | 例子 |
| :--- | :--- |
| 已解析的 profile 属性 | ``execution.py:104`` ``getattr(self.tenant, "tenant_id", "")`` |
| UI 上下文字典 | ``ui.py:436`` ``current_tenant.get("tenant_id")`` |
| 函数参数（调用方给值） | ``reconciliation.py:131``、``run_service.py:132-133``、``store.py:1081/1092/1101/1108``、``volatility_orphans.py:282/286`` |
| 别名/campaign_id 拼接 | ``order_push.py:264`` |

宽判据要么误报这 6 处，要么就得维护一张"哪种形态算合法"的允许清单 ——
那正是 Chief 自己指出的静态守卫弱点（"要持续维护哪种形态算合法"）。

**而"从 broker 读"是精确的**：生产 ``FutuPaperBroker`` **从未被赋值** ``tenant_id``，
所以任何这类读取**恒为空字符串**，拼进 key 就等于**静默丢掉租户维度** ——
两租户同 code 互相顶掉（P0-1/P0-9 的根因）。这一条规则**没有允许清单**，
也不会随代码形态漂移。

**为什么运行时 fail-closed 覆盖不到它**：``resolve_tenant`` 的 fail-closed 只约束
**经过它**的调用；而 P0-9 的原始形态是拿 ``getattr`` 的结果**直接拼 state key**，
那条路不经过 ``resolve_tenant``，运行时不会响 —— 只有这条守类守卫能拦。

**作用域（写清楚，免得被当成完整的数据流检查）**：本守卫**只拦一类回归** ——
"从 broker 读 tenant 属性"。它**不校验** state key 的租户来源是否合法
（函数参数、已解析 profile、UI 上下文都算合法来源），也**不校验**跨租户隔离是否成立
（那由 ``test_campaign_tenant_isolation_guards.py`` 的 ②③ 与迁移不变量覆盖）。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 只扫生产代码；data/qa/ 下是变异测试副本与临时目录，不是生产代码。
SCAN_DIRS = (Path("src"), Path("scripts"))

SHARED_ENTRY = {"tenant_of_broker", "tenant_id_of_broker"}
PROFILE_FN = {"resolve_tenant"}
CJK = re.compile(r"[\u4e00-\u9fff]")


# --------------------------------------------------------------------------
# 判据实现
# --------------------------------------------------------------------------


def _tenantish(name: str) -> bool:
    low = name.lower()
    return "tenant" in low or low in {"tid", "tenantid"}


def _is_brokerish(node) -> bool:
    """``broker`` / ``self.broker`` / ``_broker`` 之类。"""
    if isinstance(node, ast.Name):
        return "broker" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "broker" in node.attr.lower() or _is_brokerish(node.value)
    return False


def _call_name(node) -> str:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return ""


def _unwrap(node):
    """剥掉 ``str(...)`` / ``... or ""`` / ``...[0]`` 包装，取最内层表达式。"""
    for _ in range(8):
        if isinstance(node, ast.Call) and _call_name(node) == "str" and node.args:
            node = node.args[0]
            continue
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and node.values:
            node = node.values[0]
            continue
        if isinstance(node, ast.Subscript):
            node = node.value
            continue
        break
    return node


def _key_component_exprs(node: ast.JoinedStr) -> list[ast.expr]:
    """state-key 形态的 f-string 里用到的租户成分**表达式**。

    "key 形态"的判据：字面量含 ``:``、**不含中文**、**不含空白**。
    后两条用来排除日志/消息文案（如 ``f"三账对账发现 {n} 个标的…"``）——
    它们也含 ``:``，但不是键。

    两种成分形态都收：

    - ``Name``（``{tenant_id}``）—— 由 `_Scope.source_of` 追赋值来源；
    - ``Attribute``（``{self.tenant_id}`` / ``{self.broker.tenant_id}``）——
      **属性形态**。收它是因为"从 broker 读"这一类**在类方法里就长这样**，
      不收等于漏掉最常用的写法（同型漏洞）。
    """
    literal = "".join(
        v.value for v in node.values
        if isinstance(v, ast.Constant) and isinstance(v.value, str)
    )
    if ":" not in literal or CJK.search(literal):
        return []
    if any(ch.isspace() for ch in literal):
        return []
    out: list[ast.expr] = []
    for v in node.values:
        if not isinstance(v, ast.FormattedValue):
            continue
        expr = v.value
        if isinstance(expr, ast.Name) and _tenantish(expr.id):
            out.append(expr)
        elif isinstance(expr, ast.Attribute) and _tenantish(expr.attr):
            out.append(expr)
    return out


def _own_nodes(fn):
    """函数自身作用域内的节点 —— **不下降进嵌套函数/类/Lambda**。"""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            stack.append(child)


class _Scope:
    def __init__(self, fn):
        args = fn.args
        self.params = {a.arg for a in args.args} | {a.arg for a in args.kwonlyargs}
        if args.vararg:
            self.params.add(args.vararg.arg)
        if args.kwarg:
            self.params.add(args.kwarg.arg)
        self.assigns: dict[str, str] = {}
        self.doc = ast.get_docstring(fn) or ""

    def classify(self, expr) -> str:
        node = _unwrap(expr)
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in SHARED_ENTRY:
                return "shared"
            if name in PROFILE_FN:
                return "profile"
            if name == "getattr" and len(node.args) >= 2:
                target, key = node.args[0], node.args[1]
                if _is_brokerish(target) and isinstance(key, ast.Constant) and key.value == "tenant_id":
                    return "broker_dead"
            return "other"
        if isinstance(node, ast.Attribute):
            if node.attr == "tenant_id":
                return "broker_dead" if _is_brokerish(node.value) else "profile"
            return "other"
        if isinstance(node, ast.Name):
            if node.id in self.assigns:
                return self.assigns[node.id]
            if node.id in self.params:
                return "param"
            return "free"
        return "other"

    def record_assignments(self, fn) -> None:
        for node in _own_nodes(fn):
            target = None
            value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                target, value = node.target.id, node.value
            if target and _tenantish(target):
                self.assigns[target] = self.classify(value)

    def source_of(self, name: str) -> str:
        if name in self.assigns:
            return self.assigns[name]
        if name in self.params:
            return "param"
        return "free"


def dead_broker_reads_in_source(source: str, filename: str = "<synthetic>") -> list[tuple[int, str, str]]:
    """返回 ``(行号, 变量名, key 片段)`` —— 租户成分来自 broker 读取的那些。"""
    tree = ast.parse(source, filename=filename)
    findings: list[tuple[int, str, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope = _Scope(fn)
        scope.record_assignments(fn)
        for node in _own_nodes(fn):
            if not isinstance(node, ast.JoinedStr):
                continue
            for comp in _key_component_exprs(node):
                if scope.classify(comp) == "broker_dead":
                    literal = "".join(
                        v.value for v in node.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)
                    )
                    findings.append((node.lineno, ast.unparse(comp), literal[:60]))
    return findings


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCAN_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


# --------------------------------------------------------------------------
# 守卫
# --------------------------------------------------------------------------


def test_detector_is_not_vacuous() -> None:
    """非空转自检：检测器**必须**能检出真实的死读取。

    注意这里用的是**合成源码**而不是"树里存在某个命中"—— 因为修复后树里
    本来就没有命中，用后者做自检会变成假绿。
    """
    synthetic = (
        "def f(broker, symbol):\n"
        "    tenant_id = str(getattr(broker, \"tenant_id\", \"\") or \"\")\n"
        "    return f\"volatility_long_gamma:{tenant_id}:{symbol}\"\n"
    )
    hits = dead_broker_reads_in_source(synthetic)
    assert hits, "检测器没检出合成样本里的 broker 死读取 —— 守卫会假绿"
    assert hits[0][1] == "tenant_id"


@pytest.mark.parametrize(
    "expr",
    [
        'broker',                  # 裸名
        'self.broker',             # 类方法里的标准写法（P0-9 原缺陷的形态）
        'self._broker',
        'executor.broker',
    ],
)
def test_detector_covers_attribute_broker_forms(expr: str) -> None:
    """检测器必须覆盖 **属性形态**的 broker（``self.broker`` 那类），不只是裸名。

    为什么单列一条：``_is_brokerish`` 早期只认 ``ast.Name``，于是
    ``getattr(self.broker, "tenant_id", None)`` —— **恰恰是类内标准写法、
    也是 P0-9 原始缺陷（``DealPushHandler._tenant_id``）用的形态** —— 会漏检。
    只测裸名的自检会**绿着漏检**：自检通过 ≠ 这类被覆盖。

    Reviewer 提的正是这条：判据要覆盖"类内最常用"的形态。
    """
    synthetic = (
        "def f(symbol):\n"
        f"    tenant_id = getattr({expr}, \"tenant_id\", \"\") or \"\"\n"
        "    return f\"volatility_long_gamma:{tenant_id}:{symbol}\"\n"
    )
    hits = dead_broker_reads_in_source(synthetic)
    assert hits, (
        f"检测器漏检属性形态 `{expr}` —— 这类正是类内标准写法，"
        "而它要拦的就是 P0-9 那种回潮"
    )


def test_detector_does_not_flag_non_broker_attribute() -> None:
    """反向：``self.tenant`` / ``self.profile`` 这类**非 broker** 属性读取不得误报。"""
    synthetic = (
        "def f(symbol):\n"
        "    t = getattr(self.tenant, \"tenant_id\", \"\") or \"\"\n"
        "    p = self.profile.tenant_id\n"
        "    return f\"volatility_long_gamma:{t}:{symbol}\"\n"
    )
    assert dead_broker_reads_in_source(synthetic) == [], "非 broker 属性被误报了"


@pytest.mark.parametrize(
    "expr",
    [
        "self.tenant_id",           # 类内读 profile —— 合法
        "self.profile.tenant_id",   # 同上
    ],
)
def test_attribute_form_non_broker_is_not_flagged(expr: str) -> None:
    """属性形态的**非 broker** 成分（``{self.tenant_id}``）不得误报。"""
    synthetic = (
        "def f(symbol):\n"
        '    return f"volatility_long_gamma:{' + expr + '}:{symbol}"\n'
    )
    assert dead_broker_reads_in_source(synthetic) == [], (
        f"非 broker 属性形态 `{expr}` 被误报 —— 它是合法的 profile 读取"
    )


@pytest.mark.parametrize(
    "expr",
    [
        "self.broker.tenant_id",
        "executor.broker.tenant_id",
    ],
)
def test_attribute_form_broker_read_is_flagged(expr: str) -> None:
    """属性形态的 **broker 读取**（``{self.broker.tenant_id}``）必须命中。

    这正是 Reviewer 上一轮点出的漏检形态的**键内直读版**：
    ``getattr(self.broker, "tenant_id", None)`` 赋给变量那条已被
    ``test_detector_covers_attribute_broker_forms`` 覆盖；而**直接把
    ``self.broker.tenant_id`` 拼进 key** 是同一类的另一种写法，同样要拦。
    """
    synthetic = (
        "def f(symbol):\n"
        '    return f"volatility_long_gamma:{' + expr + '}:{symbol}"\n'
    )
    hits = dead_broker_reads_in_source(synthetic)
    assert hits, f"检测器漏检属性形态的 broker 读取 `{expr}` —— 同属'从 broker 读'这一类"


def test_detector_accepts_legit_sources() -> None:
    """反向：合法来源**不得**被误报（否则守卫不可用）。"""
    legit = (
        "def f(broker, tenant_id, symbol):\n"                                  # param
        "    a = tenant_of_broker(broker, strict=False)[0] or \"\"\n"          # 共享入口
        "    return f\"k:{a}:{symbol}\"\n"
        "def g(self, symbol):\n"                                               # 已解析 profile
        "    t = getattr(self.tenant, \"tenant_id\", \"\")\n"
        "    return f\"k:{t}:{symbol}\"\n"
    )
    assert dead_broker_reads_in_source(legit) == [], "合法来源被误报了"


def test_detector_ignores_prose() -> None:
    """含 ``:`` 的中文文案不是 state key，不得误报。"""
    prose = (
        "def f(n, tenant_id):\n"
        "    return f\"三账对账发现 {n} 个标的存在持仓差异：{tenant_id}\"\n"
    )
    assert dead_broker_reads_in_source(prose) == []


def test_no_tenant_key_component_comes_from_broker_read() -> None:
    """生产代码里，state key 的租户成分不得来自对 broker 的读取。"""
    offenders: list[str] = []
    scanned = 0
    for path in _iter_python_files():
        scanned += 1
        source = path.read_text(encoding="utf-8-sig")
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT)
        for lineno, name, key in dead_broker_reads_in_source(source, str(rel)):
            offenders.append(f"{rel}:{lineno}  变量 {name}  →  f\"{key}\"")
    assert scanned > 0, "扫描目录为空 —— 守卫会假绿"
    assert not offenders, (
        "state key 的租户成分来自对 broker 的读取 —— 该属性在生产 broker 上恒空，"
        "拼进 key 等于静默丢掉租户维度（两租户同 code 互相顶掉）。"
        "改走 tenant_of_broker(broker, strict=False)[0]：\n  " + "\n  ".join(offenders)
    )


def test_no_setattr_of_tenant_id_on_broker() -> None:
    """反向：不得给 broker 补 ``tenant_id`` 属性 —— 那会掩盖"没有租户上下文"。"""
    offenders: list[str] = []
    for path in _iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "setattr" or len(node.args) < 2:
                continue
            if isinstance(node.args[1], ast.Constant) and node.args[1].value == "tenant_id":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "不得给 broker 补 tenant_id 属性 —— 用 tenant_of_broker 解析：\n  "
        + "\n  ".join(offenders)
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
