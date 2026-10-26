"""静态守卫：关键路径禁止裸 `except: pass`（2026-10-26）。

Reviewer 要求：只扫关键路径文件、允许 `# noqa: silent-ok: 理由` 豁免、配一条自检
（塞裸 `except: pass` 必须判红），别一上来全仓扫。
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_silent_degradation as guard  # noqa: E402


def test_current_critical_files_are_clean():
    findings = guard.scan(guard.REPO_ROOT)
    assert findings == [], (
        "关键路径出现裸 except-pass："
        + "; ".join(f"{f['file']}:{f['lineno']}" for f in findings)
    )


def test_bare_except_pass_is_flagged(tmp_path: Path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8",
    )
    findings = guard.scan(tmp_path, files=("bad.py",))
    assert len(findings) == 1
    assert findings[0]["lineno"] == 4


def test_violation_makes_main_fail(tmp_path: Path, monkeypatch):
    bad_dir = tmp_path / "scripts"
    bad_dir.mkdir()
    (bad_dir / "run_service.py").write_text(
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "CRITICAL_FILES", ("scripts/run_service.py",))
    assert guard.main(["--root", str(tmp_path)]) == 1


def test_noqa_silent_ok_with_reason_is_allowed(tmp_path: Path):
    good = tmp_path / "good.py"
    good.write_text(
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:  # noqa: silent-ok: 退出路径无可补救\n"
        "        pass\n",
        encoding="utf-8",
    )
    assert guard.scan(tmp_path, files=("good.py",)) == []


def test_noqa_without_reason_is_not_enough(tmp_path: Path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:  # noqa: silent-ok\n"
        "        pass\n",
        encoding="utf-8",
    )
    assert len(guard.scan(tmp_path, files=("bad.py",))) == 1


def test_handler_with_log_is_not_flagged(tmp_path: Path):
    logged = tmp_path / "logged.py"
    logged.write_text(
        "import logging\n"
        "logger = logging.getLogger('x')\n"
        "def f():\n"
        "    try:\n"
        "        work()\n"
        "    except Exception as exc:\n"
        "        logger.error('失败：%s', exc)\n",
        encoding="utf-8",
    )
    assert guard.scan(tmp_path, files=("logged.py",)) == []


def test_missing_critical_file_is_reported(tmp_path: Path):
    findings = guard.scan(tmp_path, files=("scripts/not_there.py",))
    assert findings and "清单过期" in findings[0]["reason"]
