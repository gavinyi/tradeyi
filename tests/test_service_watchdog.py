"""守卫：外部存活检测（watchdog）—— 服务死了必须由服务之外的人喊出来。

背景（2026-11-02 事故）：生产 Worker 停了约 19 小时，期间**零告警** ——
因为告警由服务自己发出，服务死了就没人报告。本文件锁住这个补丁的三条性质：

1. **能发现四种停机形态**：无 pid、关键 pid 缺失、进程已死、日志停滞（进程在但卡死）；
2. **只读**：检测脚本不得启停任何进程、不得写库（`TradingStore(read_only=True)`）；
3. **失败必须显式**：检测本身没跑成时返回 2，不能静默当正常。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_service_alive.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_service_alive", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _data_dir(tmp_path: Path, *, pids: dict[str, str], logs: int = 1) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    for name, content in pids.items():
        (data / f"{name}.pid").write_text(content, encoding="utf-8")
    for index in range(logs):
        (data / f"service-{index}.log").write_text("log\n", encoding="utf-8")
    return data


def test_healthy_service_reports_no_problem(tmp_path):
    module = _load()
    data = _data_dir(
        tmp_path,
        pids={name: "4242" for name in module.REQUIRED_PIDS},
    )

    problems = module.collect_problems(
        data_dir=data, process_alive=lambda pid: True, log_age_minutes=lambda d: 1.0,
    )

    assert problems == []


def test_missing_pid_files_are_reported(tmp_path):
    module = _load()
    data = _data_dir(tmp_path, pids={}, logs=1)

    problems = module.collect_problems(
        data_dir=data, process_alive=lambda pid: True, log_age_minutes=lambda d: 1.0,
    )

    assert any("没有任何 pid 文件" in item for item in problems)
    assert any("缺少关键 pid 文件" in item for item in problems)


def test_dead_process_is_reported(tmp_path):
    module = _load()
    data = _data_dir(tmp_path, pids={name: "999999" for name in module.REQUIRED_PIDS})

    problems = module.collect_problems(
        data_dir=data, process_alive=lambda pid: False, log_age_minutes=lambda d: 1.0,
    )

    assert any("进程已退出" in item for item in problems)


def test_stale_log_is_reported_even_if_process_alive(tmp_path):
    """进程在、但日志 15 分钟没写 —— 卡死形态，必须报。"""
    module = _load()
    data = _data_dir(tmp_path, pids={name: "4242" for name in module.REQUIRED_PIDS})

    problems = module.collect_problems(
        data_dir=data, process_alive=lambda pid: True,
        log_age_minutes=lambda d: module.LOG_STALE_MINUTES + 5,
    )

    assert any("日志已" in item for item in problems)


def test_unparsable_pid_file_is_reported(tmp_path):
    module = _load()
    pids = {name: "4242" for name in module.REQUIRED_PIDS}
    pids["volatility-radar"] = "not-a-pid"
    data = _data_dir(tmp_path, pids=pids)

    problems = module.collect_problems(
        data_dir=data, process_alive=lambda pid: True, log_age_minutes=lambda d: 1.0,
    )

    assert any("无法解析" in item for item in problems)


def test_missing_data_dir_raises(tmp_path):
    module = _load()
    try:
        module.collect_problems(data_dir=tmp_path / "nope")
    except FileNotFoundError as exc:
        assert "数据目录不存在" in str(exc)
    else:  # pragma: no cover - 明确失败而不是静默通过
        raise AssertionError("data 目录不存在时必须抛错，不能静默当正常")


def test_script_is_read_only_and_never_starts_services():
    """红线：检测脚本只读；生产启动必须由用户从桌面双击 start.bat。"""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "TradingStore(read_only=True)" in source, "检测脚本必须以只读方式打开库"
    for banned in ("start.bat\", ", "subprocess.Popen", "schtasks /Run", "run_service"):
        assert banned not in source, f"检测脚本不得启停服务（发现 {banned}）"
    # 允许用 tasklist 探测进程，但不得调用 taskkill
    assert "taskkill" not in source


def test_docstring_documents_scheduled_task_usage():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "schtasks /Create" in source, "必须给出计划任务注册命令，否则没人会部署"
    assert "禁止" in source and "start.bat" in source, "必须写明不许自动拉起服务"


def test_docstring_is_raw_string_for_windows_paths():
    """docstring 里有 Windows 路径（`\\Tradeyi\\...`）：必须是 raw string，否则 \\T 触发转义告警。"""
    source = SCRIPT.read_text(encoding="utf-8")

    assert source.startswith('r"""'), "模块 docstring 应为 raw string，避免 \\T 这类转义告警"
    assert "Software\\Microso" not in source  # 防手滑塞进真实注册表路径
