"""子进程环境助手：保证「本仓 python 子进程」一定能 import `futu_quant`。

背景（2026-10-28 事故）：`tests/test_ml_timeseries_split.py` 的子进程测试在
`trade-test` 区假红，根因不是测试写错，而是子进程继承的环境里 `PYTHONPATH`
没有 `src`，而该 venv 又缺 editable `.pth` —— 子进程直接
`ModuleNotFoundError: futu_quant`。补 `.pth` 只是掩盖，换台机器/换 venv 会复发。

因此凡是**起本仓 python 脚本**的子进程，环境一律走这里，不手抄 `os.environ.copy()`：

- `PYTHONPATH` 前置本仓 `src`（已有则不动，不重复追加）
- `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`：Windows 下避免 GBK 代码页把中文写成乱码
- 其余变量原样继承，调用方可再覆盖

**不适用**：`taskkill` / `open` / `sqlite3` 这类非 python 子进程，别套这个助手。
"""
from __future__ import annotations

import os
from pathlib import Path

SRC_DIR = str(Path(__file__).resolve().parents[1])


def subprocess_env(
    *,
    base: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
    ensure_src: bool = True,
) -> dict[str, str]:
    """返回给子进程用的环境字典（不修改 `os.environ`）。

    `base` 缺省用 `os.environ`；`ensure_src=False` 时不注入 `PYTHONPATH`
    （给那些不需要 import 本仓的子进程用）。
    """
    env = dict(os.environ if base is None else base)
    if ensure_src:
        parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        if SRC_DIR not in parts:
            parts.insert(0, SRC_DIR)
        env["PYTHONPATH"] = os.pathsep.join(parts)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env
