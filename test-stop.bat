@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Tradeyi Test Environment Stopper

echo ========================================================
echo   停止 Tradeyi 独立测试环境 (仿真实验区)
echo ========================================================
echo.

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 未找到虚拟环境 Python: %PY%
    echo.
    pause
    exit /b 1
)

echo 正在安全停止测试环境专属服务（绝不影响生产环境）...
"%PY%" scripts\test_env.py stop
echo.
echo ========================================================
echo   测试环境专属服务已全部停止。
echo ========================================================
echo.
pause