"""SDK 日志轮转与按租户分离 —— 防止 `data/futu-logs/` 无限膨胀。

2026-10-30 实测：`futu-service.log` 6 小时涨到 761MB，整个日志目录 915MB。
两个原因叠在一起：

1. 裸 ``logging.FileHandler`` 没有任何轮转，写多少涨多少；
2. 两个租户的 worker 都用 ``FUTU_LOG_ROLE=service``，同写一份文件 ——
   在 Windows 上既分不清谁在刷屏，也没法安全轮转（rename 被另一个进程
   的打开句柄挡住，直接 PermissionError）。

所以这里同时锁两件事：handler 必须是 Rotating 的，role 必须带租户。
"""

import logging
import logging.handlers
import os

from pathlib import Path

from futu_quant.broker import configure_futu_process_log

HERE = Path(__file__).resolve().parents[1]


def _file_handler(target: Path):
    """取 SDK 文件 logger 上刚装配的那个 handler。"""
    from futu.common.ft_logger import FTLog

    logger = FTLog().file_logger
    return next(h for h in logger.handlers if getattr(h, "baseFilename", "") == str(target))


def test_windows_handler_rotates_by_size(tmp_path, monkeypatch):
    """Windows 上必须是按大小轮转的 handler，不能是裸 FileHandler。"""
    if os.name != "nt":
        return
    monkeypatch.setenv("FUTU_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("FUTU_LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("FUTU_LOG_BACKUPS", raising=False)

    target = configure_futu_process_log("rotation-test")
    handler = _file_handler(target)

    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == 32 * 1024 * 1024
    assert handler.backupCount == 4


def test_rotation_limits_are_env_overridable(tmp_path, monkeypatch):
    """上限可调：换算关系必须真的落到 handler 上，别只写在注释里。"""
    if os.name != "nt":
        return
    monkeypatch.setenv("FUTU_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("FUTU_LOG_MAX_BYTES", "1048576")
    monkeypatch.setenv("FUTU_LOG_BACKUPS", "9")

    target = configure_futu_process_log("rotation-env-test")
    handler = _file_handler(target)

    assert handler.maxBytes == 1024 * 1024
    assert handler.backupCount == 9


def test_worker_role_carries_tenant():
    """两个 worker 不得同写一份日志：role 必须带租户名。

    源码级断言（同 `test_stop_limit_ui_explicit` 的风格）—— 这里要锁的是
    「main 里在 broker 初始化之前就把 role 改成带租户的」，用行为测试会
    需要起整个调度循环，代价远大于收益。
    """
    src = (HERE / "scripts" / "run_service.py").read_text(encoding="utf-8")
    start = src.index("args = parser.parse_args()")
    end = src.index("settings = Settings.load(refresh=True)", start)
    between = src[start:end]
    assert 'os.environ["FUTU_LOG_ROLE"] = f"service-{args.tenant}"' in between, (
        "worker 的 SDK 日志 role 未按租户区分，或设置得太晚"
        "（必须在首个 broker 实例化之前，否则文件句柄已被旧 role 拿走）"
    )
