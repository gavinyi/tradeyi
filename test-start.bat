@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Tradeyi Test Environment (Port 8601)

echo ========================================================
echo   启动 Tradeyi 独立测试环境 (仿真实验区)
echo ========================================================
echo.
echo 工作目录: %~dp0
echo 数据库:   %~dp0data\trading.db
echo 访问端口: http://127.0.0.1:8601
echo 仅挂载租户: futu_paper_sandbox (SIMULATE 模拟盘)
echo.

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 未找到虚拟环境 Python: %PY%
    echo 请确认处于 E:\Thisyi\trade-test 目录下
    echo.
    pause
    exit /b 1
)

echo 正在通过 test_env.py 拉起测试环境服务...
"%PY%" scripts\test_env.py start
if errorlevel 1 (
    echo.
    echo [错误] 测试环境启动失败，请检查上方日志！
    echo.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   测试环境已成功启动！
echo   请在浏览器打开: http://127.0.0.1:8601
echo ========================================================
echo.
"%PY%" scripts\test_env.py status
echo.
pause