"""P1-05 清理守卫：删除「假重训」陷阱脚本 + 统一 `_print_safe` 签名。

## 1. 删除陷阱脚本

曾有一个「月度重训」脚本**只刷元数据、从不训练**：它把 `volatility_scorer_v3.json`
的 `trained_at` 写成当前时间、pkl 却不动，导致「json 显示 10-14、pkl 停在 09-09」
的假象持续 6 周。名字像训练器比代码错更难查 → 直接删除（P1-05），并清掉全仓引用，
否则文档/注释还在描述一个不存在的脚本，下一个人又会去找它。

变异：把脚本名或引用塞回文本 → 守卫必须红。

## 2. `_print_safe` 统一为 `*args, **kwargs`

`ml_export_training_data_v3.py` 里它是全仓唯一 strict 单参版本；调用点一旦写多参数
就 TypeError。统一放宽，并用 GBK 流做**行为**验证（不是只看源码）。
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for extra in (str(ROOT), str(SCRIPTS)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

TRAP_SCRIPT = ROOT / "scripts/run_monthly_ml_retrain.py"
TRAP_NAME = "run_monthly_ml_retrain"
EXPORT_SCRIPT = ROOT / "scripts/ml_export_training_data_v3.py"
SCAN_DIRS = ("src", "scripts", "tests", "pages", "docs")
SCAN_GLOBS = ("*.md", "*.py", "*.bat", "*.txt")
# `changelog.md` 是**历史流水账**：记录的是「当时删了什么」，允许提到被删对象。
HISTORICAL_FILES = {"changelog.md"}


def _repo_files():
    yield from ROOT.glob("*.md")
    yield from ROOT.glob("*.py")
    # 根目录启动脚本（start.bat/stop.bat 等）也可能提到已删脚本，必须一并扫
    yield from ROOT.glob("*.bat")
    for directory in SCAN_DIRS:
        base = ROOT / directory
        if not base.exists():
            continue
        for pattern in SCAN_GLOBS:
            yield from base.rglob(pattern)


def _assert_no_trap_references(text: str, *, label: str) -> None:
    assert TRAP_NAME not in text, (
        f"{label} 仍在引用已删除的陷阱脚本 {TRAP_NAME}："
        "P1-05 已删除它，文档/注释必须同步（否则下一个人会去找不存在的脚本）"
    )


def test_trap_script_is_deleted():
    assert not TRAP_SCRIPT.exists(), (
        f"{TRAP_SCRIPT} 仍存在：该脚本只刷元数据不训练，已按 P1-05 删除"
    )


def test_no_references_to_deleted_trap_script():
    offenders = []
    for path in _repo_files():
        if path.resolve() == Path(__file__).resolve():
            continue  # 守卫自身必然提到名字
        if path.name in HISTORICAL_FILES:
            continue  # 历史 changelog 允许记录「删除了什么」
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if TRAP_NAME in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"这些文件仍引用已删除的陷阱脚本：{offenders}"


def test_scan_covers_root_bat_files():
    """根目录 .bat 必须进扫描范围（start.bat/stop.bat 也可能提到已删脚本）。

    Reviewer 2026-10-25 复核指出：原实现只 glob 根目录 `*.md/*.py`，漏了 `.bat`。
    """
    scanned = {path.name for path in _repo_files()}
    root_bat_files = {path.name for path in ROOT.glob("*.bat")}
    assert root_bat_files, "仓库根目录没有 .bat？扫描覆盖断言需要至少一个样本"
    missing = root_bat_files - scanned
    assert not missing, f"根目录 .bat 未被扫描：{missing}"


@pytest.mark.parametrize(
    "sample",
    [
        "scripts/" + "run_monthly_ml_retrain" + ".py",
        "python " + "run_monthly_ml_retrain" + ".py --once",
    ],
)
def test_mutation_reintroduced_reference_must_be_red(sample):
    with pytest.raises(AssertionError):
        _assert_no_trap_references(f"docs: {sample}", label="mutation")
    _assert_no_trap_references("普通文档，无引用", label="clean")


# --------------------------------------------------------- _print_safe 契约
def _print_safe_node() -> ast.FunctionDef:
    tree = ast.parse(EXPORT_SCRIPT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_print_safe":
            return node
    raise AssertionError("ml_export_training_data_v3.py 找不到 _print_safe")


def test_export_print_safe_accepts_varargs_and_kwargs():
    """签名必须是 `*args, **kwargs`：调用点多参数时不许 TypeError。"""
    node = _print_safe_node()
    assert node.args.vararg is not None, "_print_safe 缺少 *args"
    assert node.args.kwarg is not None, "_print_safe 缺少 **kwargs"
    assert not node.args.args, "_print_safe 不应再有 strict 单参位置参数"


def test_export_print_safe_survives_gbk_stdout_with_multiple_args(tmp_path):
    """行为守卫：GBK 流上打印 emoji + 多参数，必须不抛 UnicodeEncodeError。"""
    import ml_export_training_data_v3 as exporter

    raw = io.BytesIO()
    gbk_stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
    original = sys.stdout
    try:
        sys.stdout = gbk_stream
        exporter._print_safe("\u26a0", "multi", "args", sep="|")  # ⚠
        gbk_stream.flush()
    finally:
        sys.stdout = original
    assert raw.getvalue(), "GBK 流上没有任何输出（helper 未生效）"


def test_mutation_export_strict_signature_must_be_red():
    """变异：签名退回 strict 单参 → 源码契约断言必须红。"""
    source = EXPORT_SCRIPT.read_text(encoding="utf-8")
    mutated = source.replace(
        "def _print_safe(*args, **kwargs) -> None:",
        "def _print_safe(message: str) -> None:",
        1,
    )
    assert mutated != source, "变异目标不存在：源码已不是 *args/**kwargs 签名"
    tree = ast.parse(mutated)
    node = next(
        item for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef) and item.name == "_print_safe"
    )
    assert node.args.vararg is None, "变异后仍是 *args → 守卫抓不到 strict 单参回退"
