"""停止信号语义守卫：租户级 stop 与全局 stop 都必须能唤醒 _wait_or_stop。

背景（Ops 实测、Coder 修复）：
`run_service._wait_or_stop()` 原先只轮询全局 `data/trading-service.stop`，
而 `main()` 算出的租户级 `data/trading-service-<tenant>.stop` 从不参与判定，
且全项目没有任何代码写它 —— 于是「优雅停止单个租户」根本做不到；
反过来，全局 stop 文件一旦残留就会杀掉**所有**租户 Worker（含实盘真金），
而 `launch_workers.py` 从不清理它。

本文件锁定三条契约：
1. 租户级 stop 文件必须能唤醒等待（单租户可停）；
2. 全局 stop 文件必须仍能唤醒等待（总闸语义不变）；
3. 独立脚本不得复用全局 stop 文件名，否则会跨脚本误伤全部租户。
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_service  # noqa: E402
import run_volatility_strategy  # noqa: E402


def _elapsed(fn) -> tuple[float, bool]:
    """跑一次等待，返回 (耗时, 是否被 stop 信号唤醒)。"""
    started = time.monotonic()
    fn()
    return time.monotonic() - started, run_service.STOP_EVENT.is_set()


def test_tenant_level_stop_file_wakes_the_wait(tmp_path: Path, monkeypatch):
    """契约 1：租户级 stop 必须能唤醒 _wait_or_stop（否则单租户无法优雅停止）。"""
    global_stop = tmp_path / "trading-service.stop"
    tenant_stop = tmp_path / "trading-service-futu_real_master.stop"
    monkeypatch.setattr(run_service, "STOP_FILE", global_stop)
    monkeypatch.setattr(run_service, "STOP_EVENT", threading.Event())

    tenant_stop.touch()
    elapsed, signalled = _elapsed(
        lambda: run_service._wait_or_stop(5.0, tenant_stop)
    )

    assert signalled is True, "租户级 stop 文件未能唤醒 _wait_or_stop，单租户停止不可用"
    assert elapsed < 1.0, f"唤醒过慢（{elapsed:.2f}s），停止请求必须即时生效"


def test_global_stop_file_still_wakes_the_wait(tmp_path: Path, monkeypatch):
    """契约 2：全局 stop 必须仍能唤醒等待（UI 停止按钮 = 停全部，语义不变）。"""
    global_stop = tmp_path / "trading-service.stop"
    tenant_stop = tmp_path / "trading-service-futu_real_master.stop"
    monkeypatch.setattr(run_service, "STOP_FILE", global_stop)
    monkeypatch.setattr(run_service, "STOP_EVENT", threading.Event())

    global_stop.touch()
    elapsed, signalled = _elapsed(
        lambda: run_service._wait_or_stop(5.0, tenant_stop)
    )

    assert signalled is True, "全局 stop 文件未能唤醒 _wait_or_stop，总闸失效"
    assert elapsed < 1.0, f"唤醒过慢（{elapsed:.2f}s）"


def test_no_stop_file_waits_the_full_duration(tmp_path: Path, monkeypatch):
    """契约 1/2 的反面：两个 stop 文件都不存在时必须等满，不能误判为已停止。"""
    monkeypatch.setattr(run_service, "STOP_FILE", tmp_path / "trading-service.stop")
    monkeypatch.setattr(run_service, "STOP_EVENT", threading.Event())

    elapsed, signalled = _elapsed(
        lambda: run_service._wait_or_stop(0.4, tmp_path / "trading-service-futu_real_master.stop")
    )

    assert signalled is False, "无 stop 文件却报告已停止，会造成立即自杀"
    assert elapsed >= 0.35, f"未等满即返回（{elapsed:.2f}s），轮询节奏被破坏"


def test_independent_script_does_not_reuse_global_stop_name():
    """契约 3：独立脚本不得复用全局 stop 文件名，否则会跨脚本误伤全部租户。"""
    assert run_volatility_strategy.STOP_FILE != run_service.STOP_FILE, (
        f"run_volatility_strategy 复用了全局 stop 路径 "
        f"{run_volatility_strategy.STOP_FILE}，会顺带停掉全部租户 Worker"
    )
    assert run_volatility_strategy.RESTART_FILE != run_service.RESTART_FILE, (
        "run_volatility_strategy 复用了全局 restart 路径，会触发全部租户重启"
    )


def test_main_clears_stale_global_stop_on_startup():
    """契约 2 的启动侧：全局 stop 残留必须被清掉，否则 start.bat 静默拉起失败。

    静态守卫（与 test_launch_approval_page_passes_tenant_and_account_to_sync 同风格）：
    main() 必须在进入服务循环前 unlink 全局 STOP_FILE。
    """
    source = Path("scripts/run_service.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    main_fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    # 找到 main() 里第一处对全局 STOP_FILE 的 unlink 调用
    unlink_calls = [
        node for node in ast.walk(main_fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "unlink"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "STOP_FILE"
    ]
    assert unlink_calls, (
        "main() 未清除残留的全局 STOP_FILE："
        "强杀进程留下的 stop 文件会让新 Worker 第一次 _wait_or_stop 就自杀，"
        "start.bat 将静默拉起失败"
    )
