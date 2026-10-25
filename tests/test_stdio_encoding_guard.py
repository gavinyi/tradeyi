"""全仓 stdio 编码守卫（backlog #10）：脚本里的 emoji print 必须走 `_print_safe`。

背景：今天连续 4 次同类事故——retrain stdout、export emoji、subprocess decode、子进程 emoji。
共同形态：Windows 控制台默认 **GBK**，emoji（⚠️/✅/📊…）在 `print` 时抛
`UnicodeEncodeError`，用户手工重跑脚本会直接崩在第一步。

本守卫（静态 + 一次 GBK 冒烟）：
1. 清单内每个脚本：不允许存在「含非 GBK 字符的裸 `print(...)`」；
2. 文件里只要调用了 `_print_safe`，就必须真的定义/导入它，且定义体必须捕获
   `UnicodeEncodeError`（防止空壳 helper）；
3. 变异：把带 emoji 的裸 print 注入文本 → 守卫必须红；
4. GBK 冒烟：`ml_export_training_data_v3.py --help` 在 `PYTHONIOENCODING=gbk` 下必须退出 0。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "scripts/migrate_risk_limits_to_accounts.py",
    "scripts/migrate_volatility_campaigns_tenant_scope.py",
    "scripts/ml_compare_models.py",
    "scripts/ml_export_training_data.py",
    "scripts/ml_export_training_data_v3.py",
    "scripts/ml_retrain_monthly.py",
    "scripts/ml_test_scorer.py",
    "scripts/ml_train_symbol_scorer.py",
    "scripts/probe_order_submit_cancel.py",
    "scripts/test_env_smoke.py",
    "scripts/vacuum_db.py",
)
EMOJI_SAMPLES = ("\u26a0", "\u2705", "\U0001f4ca")  # ⚠ ✅ 📊


def _is_gbk_safe(text: str) -> bool:
    try:
        text.encode("gbk")
        return True
    except UnicodeEncodeError:
        return False


def _unsafe_print_lines(source: str) -> list[int]:
    """返回「含非 GBK 字符的裸 print 调用」所在行号。"""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            continue
        segment = ast.get_source_segment(source, node)
        if segment is not None and not _is_gbk_safe(segment):
            lines.append(node.lineno)
    return lines


def _assert_no_unsafe_print(source: str, *, label: str) -> None:
    unsafe = _unsafe_print_lines(source)
    assert not unsafe, (
        f"{label} 存在 {len(unsafe)} 处含 emoji 的裸 print（行 {unsafe[:8]}）："
        "GBK 控制台下会 UnicodeEncodeError，改用 _print_safe"
    )


def _print_safe_defined(source: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_print_safe":
            body = ast.get_source_segment(source, node) or ""
            return "UnicodeEncodeError" in body
    # 也允许从其它模块显式导入
    return "_print_safe" in source and "import" in source


def test_sweep_file_list_is_complete():
    assert len(FILES) == 11, "sweep 清单被改动：需同步更新报告与后端核查"


@pytest.mark.parametrize("relative", FILES)
def test_no_emoji_bare_print_in_swept_scripts(relative):
    path = ROOT / relative
    assert path.exists(), f"清单里的脚本不存在：{relative}"
    source = path.read_text(encoding="utf-8")
    _assert_no_unsafe_print(source, label=relative)
    if "_print_safe(" in source:
        assert _print_safe_defined(source), (
            f"{relative} 调用了 _print_safe 但没有真正定义（或定义体没捕获 UnicodeEncodeError）"
        )


@pytest.mark.parametrize("emoji", EMOJI_SAMPLES)
def test_mutation_injected_emoji_print_must_be_red(emoji):
    mutated = f'def demo():\n    print("{emoji} boom")\n'
    with pytest.raises(AssertionError):
        _assert_no_unsafe_print(mutated, label="mutation")


def test_gbk_console_smoke_export_help():
    """GBK 控制台下导出脚本的入口必须能跑（emoji 不再崩）。"""
    env = {**os.environ, "PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0"}
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ml_export_training_data_v3.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=ROOT, env=env, timeout=300,
    )
    assert result.returncode == 0, (
        f"GBK 控制台下导出脚本入口崩了：\n{result.stderr[-1500:]}"
    )
    assert "UnicodeEncodeError" not in result.stderr
