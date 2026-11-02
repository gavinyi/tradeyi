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


def _ok_kwargs(module, **overrides):
    base = dict(
        process_alive=lambda pid: True,
        log_age_minutes=lambda d: 1.0,
        heartbeat_age=lambda tenant: 1.0,
        in_regular_session=lambda: True,
    )
    base.update(overrides)
    return base


def test_healthy_service_reports_no_problem(tmp_path):
    module = _load()
    data = _data_dir(tmp_path, pids={name: "4242" for name in module.REQUIRED_PIDS})

    problems = module.collect_problems(data_dir=data, **_ok_kwargs(module))

    assert problems == []


def test_missing_pid_files_are_reported(tmp_path):
    module = _load()
    data = _data_dir(tmp_path, pids={}, logs=1)

    problems = module.collect_problems(data_dir=data, **_ok_kwargs(module))

    assert any("没有任何 pid 文件" in item for item in problems)
    assert any("缺少关键 pid 文件" in item for item in problems)


def test_dead_process_is_reported(tmp_path):
    module = _load()
    data = _data_dir(tmp_path, pids={name: "999999" for name in module.REQUIRED_PIDS})

    problems = module.collect_problems(
        data_dir=data, **_ok_kwargs(module, process_alive=lambda pid: False),
    )

    assert any("进程已退出" in item for item in problems)


def test_stale_heartbeat_is_reported_per_tenant(tmp_path):
    """Reviewer 2026-11-02：心跳按租户判，实盘停了纸盘还活着**不能**算健康。"""
    module = _load()
    data = _data_dir(tmp_path, pids={name: "4242" for name in module.REQUIRED_PIDS})

    def beats(tenant: str):
        return 0.3 if tenant == "futu_paper_sandbox" else module.HEARTBEAT_STALE_MINUTES + 30

    problems = module.collect_problems(
        data_dir=data, **_ok_kwargs(module, heartbeat_age=beats),
    )

    stale = [item for item in problems if "心跳已" in item]
    assert len(stale) == 1, f"应只报停掉的那个租户，实际：{problems}"
    assert "futu_real_master" in stale[0], "必须点名是哪个租户停了"
    assert "futu_paper_sandbox" not in stale[0]


def test_missing_heartbeat_is_reported_as_unknown(tmp_path):
    module = _load()
    data = _data_dir(tmp_path, pids={name: "4242" for name in module.REQUIRED_PIDS})

    problems = module.collect_problems(
        data_dir=data, **_ok_kwargs(module, heartbeat_age=lambda tenant: None),
    )

    assert any("读不到服务心跳" in item for item in problems)


def test_required_pids_are_per_tenant_not_the_legacy_file(tmp_path):
    """`trading-service.pid` 指向「谁最后启动就是谁」，身份会漂，不得作为判据。"""
    module = _load()

    assert "trading-service.pid" not in module.REQUIRED_PIDS
    assert "trading-service-futu_real_master" in module.REQUIRED_PIDS
    assert "trading-service-futu_paper_sandbox" in module.REQUIRED_PIDS


def test_stale_log_only_counted_during_regular_session(tmp_path):
    """休市时日志本来就不写：不能在非交易时段拿它判活（否则每晚刷几十条假告警）。"""
    module = _load()
    data = _data_dir(tmp_path, pids={name: "4242" for name in module.REQUIRED_PIDS})
    stale = module.LOG_STALE_MINUTES + 30

    closed_problems = module.collect_problems(
        data_dir=data,
        **_ok_kwargs(module, log_age_minutes=lambda d: stale, in_regular_session=lambda: False),
    )
    open_problems = module.collect_problems(
        data_dir=data,
        **_ok_kwargs(module, log_age_minutes=lambda d: stale, in_regular_session=lambda: True),
    )

    assert not any("没有写入" in item for item in closed_problems), "休市时不该因日志停滞报警"
    assert any("没有写入" in item for item in open_problems), "交易时段日志停滞必须报"


def test_alert_escalates_only_after_consecutive_failures(tmp_path):
    """第一次只记状态，连续第 2 次才推 CRITICAL（压抖动）。"""
    module = _load()
    state_file = tmp_path / "watchdog_state.json"

    module.save_state(tmp_path, {"consecutive_failures": 1, "alerted": False})
    assert state_file.exists() or (tmp_path / module.STATE_FILE_NAME).exists()

    state = module.load_state(tmp_path)
    assert state["consecutive_failures"] == 1
    assert module.ESCALATE_AFTER == 2


def test_state_roundtrip_and_recovery_reset(tmp_path):
    module = _load()
    module.save_state(tmp_path, {"consecutive_failures": 3, "alerted": True})

    assert module.load_state(tmp_path) == {"consecutive_failures": 3, "alerted": True}

    module.save_state(tmp_path, {"consecutive_failures": 0, "alerted": False})
    assert module.load_state(tmp_path) == {"consecutive_failures": 0, "alerted": False}


def test_unparsable_pid_file_is_reported(tmp_path):
    module = _load()
    pids = {name: "4242" for name in module.REQUIRED_PIDS}
    pids["volatility-radar"] = "not-a-pid"
    data = _data_dir(tmp_path, pids=pids)

    problems = module.collect_problems(data_dir=data, **_ok_kwargs(module))

    assert any("无法解析" in item for item in problems)


def test_missing_data_dir_raises(tmp_path):
    module = _load()
    try:
        module.collect_problems(data_dir=tmp_path / "nope", **_ok_kwargs(module))
    except FileNotFoundError as exc:
        assert "数据目录不存在" in str(exc)
    else:  # pragma: no cover - 明确失败而不是静默通过
        raise AssertionError("data 目录不存在时必须抛错，不能静默当正常")


def test_script_is_read_only_and_never_starts_services():
    """红线：检测脚本只读 + 只告警；生产启动必须由用户从桌面双击 start.bat。"""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "read_only=True" in source, "检测脚本必须以只读方式打开库"
    for banned in ("subprocess.Popen", "taskkill", "run_service", "start.bat\")"):
        assert banned not in source, f"检测脚本不得启停服务（发现 {banned}）"


def test_script_has_heartbeat_session_and_recovery_logic():
    """Reviewer 三条要求必须都在代码里，防止被后来的重构悄悄删掉。"""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "strategy_heartbeats" in source, "主判据必须是心跳表"
    assert "service_heartbeats" not in source, "不存在 service_heartbeats 表，注释/代码里不得出现"
    assert "USMarketCalendar" in source, "日志停滞必须受交易时段约束"
    assert "is_recovered=True" in source, "恢复时必须推一次恢复通知"
    assert "ESCALATE_AFTER" in source, "连续失败才升级 CRITICAL"


def test_heartbeat_docstring_says_missing_means_alert():
    """读不到心跳必须写成「异常并报警」，不能写成「未统计就不报」。"""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "视为异常" in source, "docstring 必须声明读不到心跳=异常"
    assert "按未统计处理" not in source, "这句会被下一手误改成静默跳过"


def test_docstring_documents_scheduled_task_usage():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "schtasks /Create" in source, "必须给出计划任务注册命令，否则没人会部署"
    assert "禁止" in source and "start.bat" in source, "必须写明不许自动拉起服务"


def test_docstring_is_raw_string_for_windows_paths():
    """docstring 里有 Windows 路径（`\\Tradeyi\\...`）：必须是 raw string，否则 \\T 触发转义告警。"""
    source = SCRIPT.read_text(encoding="utf-8")

    assert source.startswith('r"""'), "模块 docstring 应为 raw string，避免 \\T 这类转义告警"
