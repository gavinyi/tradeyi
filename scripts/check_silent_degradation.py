"""关键路径「静默降级」静态守卫（2026-10-26）。

只扫交易 / 风控 / 资金 / 行情关键路径文件，禁止裸 `except: pass` 把失败折叠成「看起来成功」。
允许例外，但必须显式豁免并写理由：

    except Exception:
        pass  # noqa: silent-ok: 这里丢弃的原因是……

用法：
    python scripts/check_silent_degradation.py        # 扫默认清单，有违规 exit 1
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]

# Ops 的静默降级清单（data/qa/silent_degradation_inventory.md）落在这些文件里。
# 只扫关键路径，不搞全仓扫描（Reviewer：别一上来就全仓扫）。
CRITICAL_FILES: tuple[str, ...] = (
    "scripts/run_service.py",
    "scripts/run_gamma_strategy.py",
    "src/futu_quant/option_history.py",
    "src/futu_quant/broker.py",
    "src/futu_quant/execution.py",
    "src/futu_quant/volatility_scanner.py",
    "src/futu_quant/reconciliation.py",
    "src/futu_quant/risk.py",
    "src/futu_quant/store.py",
    "src/futu_quant/alerts.py",
)

ALLOW_MARKER = "noqa: silent-ok:"


def _handler_is_silent(node: ast.ExceptHandler) -> bool:
    """handler 体里除 `pass` / 常量表达式外没有别的语句 → 视为静默吞异常。"""
    meaningful = [
        statement for statement in node.body
        if not isinstance(statement, ast.Pass)
        and not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
        )
    ]
    return not meaningful


def find_violations(path: Path) -> list[dict]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # 语法错误交给 ruff/pytest，这里不误判
        return [{
            "lineno": exc.lineno or 0, "column": 0,
            "reason": f"无法解析（{exc.msg}）", "snippet": "",
        }]
    violations: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _handler_is_silent(node):
            continue
        end = getattr(node.body[-1], "end_lineno", node.lineno) if node.body else node.lineno
        block = "\n".join(lines[node.lineno - 1:end])
        if ALLOW_MARKER in block:
            continue
        violations.append({
            "lineno": node.lineno,
            "column": node.col_offset,
            "reason": "裸 except 静默吞异常（补日志/计数，或加 # noqa: silent-ok: 理由）",
            "snippet": lines[node.lineno - 1].strip(),
        })
    return violations


def scan(root: Path = REPO_ROOT, files: tuple[str, ...] = CRITICAL_FILES) -> list[dict]:
    findings: list[dict] = []
    for relative in files:
        path = root / relative
        if not path.exists():
            findings.append({
                "file": relative, "lineno": 0, "column": 0,
                "reason": "清单里的关键文件不存在（清单过期）", "snippet": "",
            })
            continue
        for violation in find_violations(path):
            findings.append({"file": relative, **violation})
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="关键路径静默降级静态守卫")
    parser.add_argument("--root", default=str(REPO_ROOT), help="仓库根目录")
    args = parser.parse_args(argv)

    findings = scan(Path(args.root))
    if not findings:
        print(f"静默降级守卫通过：{len(CRITICAL_FILES)} 个关键文件无裸 except-pass")
        return 0
    for finding in findings:
        print(
            f"{finding['file']}:{finding['lineno']}: {finding['reason']}  "
            f"| {finding['snippet']}"
        )
    print(f"共 {len(findings)} 处违规")
    return 1


if __name__ == "__main__":
    sys.exit(main())
