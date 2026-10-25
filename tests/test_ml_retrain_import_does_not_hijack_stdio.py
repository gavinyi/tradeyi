"""守卫：导入 `scripts.ml_retrain_monthly` 不得替换 `sys.stdout` / `sys.stderr`。

背景（2026-10-25 实测、Reviewer 定位根因）：该脚本原先在 **import 期** 把
`sys.stdout`/`sys.stderr` 换成 `io.TextIOWrapper`。任何 `import scripts.ml_retrain_monthly`
的测试（如 `tests/test_ml_auc_gate.py`）都会把 pytest 的 capture 对象顶掉，teardown
去读已关闭的文件 → `ValueError: I/O operation on closed file`：默认 capture 下
`pytest tests --collect-only` 收到 393 条就崩、退出码不可信，全量根本跑不起来。

修复：把那 4 行挪进 `_force_utf8_stdio()`，只在 `if __name__ == "__main__":` 里调用。

变异（必须转红）：把 `_force_utf8_stdio()` 的调用挪回模块级（import 期执行），
本文件两条用例都要变红。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_import_does_not_replace_standard_streams():
    stdout_before, stderr_before = sys.stdout, sys.stderr

    module = importlib.import_module("scripts.ml_retrain_monthly")

    assert sys.stdout is stdout_before, "导入期替换了 sys.stdout：会顶掉 pytest capture"
    assert sys.stderr is stderr_before, "导入期替换了 sys.stderr：会顶掉 pytest capture"
    assert callable(module._force_utf8_stdio), "入口专用包装函数应保留"


def test_module_level_source_never_calls_stdio_wrapper():
    module = importlib.import_module("scripts.ml_retrain_monthly")
    source = Path(module.__file__).read_text(encoding="utf-8")
    module_level = source.split('if __name__ == "__main__":')[0]
    module_level_lines = [line.strip() for line in module_level.splitlines()]

    assert "_force_utf8_stdio()" not in module_level_lines, (
        "模块级调用了 _force_utf8_stdio()：import 即接管 stdio，pytest capture 必崩"
    )
    assert "_force_utf8_stdio()" in source.split('if __name__ == "__main__":')[1], (
        "入口块必须调用 _force_utf8_stdio()，否则 Windows 下日志中文乱码"
    )
