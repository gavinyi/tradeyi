"""写侧版本 bump 与历史落库统一守卫（P1-②）。

口径（Reviewer 可审）：
1. **每条 runtime_config 写路径必须同时落 config_versions**。
   代码里允许写 runtime_config 的只有两处入口：
   - `set_runtime_config` / `mutate_runtime_config`（都调用 `record_config_version`）
   - `publish_config_release_with_audit` / `rollback_config_release_with_audit`
     （事务内直接 INSERT config_versions）
   静态守卫钉「写 runtime_config 的函数体内必须出现 config_versions 的写入」，
   防止再出现「版本涨了、历史没记」的缺口键（CORE_SATELLITE_DAILY_VALIDATION v12 / GAMMA_CAMPAIGNS v14 事故）。
2. **表名唯一**：历史表只有 `config_versions`。守卫全仓扫描禁止出现历史表别名
   字样（Ops 2026-11-05 发现的命名混乱源；别名拼写见守卫内的拼接式）。
3. **补写脚本只读增量**：backfill 脚本只 INSERT config_versions、不改
   runtime_config 值与 version（补留痕不算版本变更）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "src" / "futu_quant" / "store.py"

# 运行时权威表的写入 SQL 片段（出现即视为一次 runtime_config 写）。
_RUNTIME_WRITE_MARKERS = ("INSERT INTO runtime_config", "UPDATE runtime_config")
# 历史表的写入 SQL 片段。
_HISTORY_WRITE_MARKERS = ("INSERT INTO config_versions",)


def _function_defs(tree: ast.AST) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


def _sql_strings(node: ast.AST) -> list[str]:
    """收集函数体内全部字符串常量（SQL 是字符串，AST 字面量层即可判）。"""
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _walk_calls_for(node: ast.AST, names: set[str]) -> bool:
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            label = getattr(func, "attr", "") or getattr(func, "id", "")
            if label in names:
                return True
    return False


def test_runtime_config_writes_also_record_history():
    """写 runtime_config 的每个函数，必须同时写 config_versions（直接写或经委托）。"""
    tree = ast.parse(STORE.read_text(encoding="utf-8"), filename=str(STORE))
    offenders: list[str] = []
    delegated = {"record_config_version"}
    for name, fn in _function_defs(tree).items():
        sqls = "\n".join(_sql_strings(fn))
        writes_runtime = any(marker in sqls for marker in _RUNTIME_WRITE_MARKERS)
        if not writes_runtime:
            continue
        writes_history = any(marker in sqls for marker in _HISTORY_WRITE_MARKERS)
        delegates = _walk_calls_for(fn, delegated)
        if not (writes_history or delegates):
            offenders.append(name)
    assert not offenders, (
        f"以下函数写 runtime_config 却不落 config_versions 历史（P1-② 缺口形态）：{offenders}"
    )


def test_no_forbidden_history_table_alias():
    """全仓 Python 源码禁止出现历史表别名（唯一历史表 = config_versions）。

    别名字面量在本守卫里按「config + _history」拼出，避免守卫文本自红。
    """
    forbidden = "config" + "_history"
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("data/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if forbidden in text:
            offenders.append(relative)
    assert not offenders, f"出现历史表别名（应统一为 config_versions）：{offenders}"


def test_backfill_script_never_touches_runtime_config():
    """补写脚本只 INSERT config_versions，禁止写 runtime_config（补留痕≠版本变更）。"""
    script = ROOT / "scripts" / ("backfill" + "_config_gaps.py")
    assert script.exists(), "补写脚本必须存在（P1-② 交付物）"
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    sqls = "\n".join(_sql_strings(tree))
    assert "INSERT INTO config_versions" in sqls, "补写脚本必须落 config_versions"
    assert "INSERT INTO runtime_config" not in sqls, "补写脚本禁止改 runtime_config（只补历史）"
    assert "UPDATE runtime_config" not in sqls, "补写脚本禁止改 runtime_config（只补历史）"
