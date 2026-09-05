@echo off
REM 富途量化交易系统 - Windows 停止脚本

echo ========================================
echo 停止富途量化交易系统
echo ========================================
echo.

REM 1. 停止交易服务
echo [1/2] 停止交易服务...

if exist "data\trading-service.pid" (
    set /p PID=<data\trading-service.pid
    echo   正在停止服务 (PID: %PID%)...
    taskkill /PID %PID% /F >nul 2>&1
    if errorlevel 0 (
        echo   ✓ 交易服务已停止
        del data\trading-service.pid >nul 2>&1
    ) else (
        echo   ⚠ 无法停止服务（进程可能已结束）
    )
) else (
    echo   ⚠ 未找到 PID 文件，尝试按进程名停止...
    taskkill /F /FI "WINDOWTITLE eq Trading Service*" >nul 2>&1
)

REM 2. 停止 Streamlit UI
echo [2/2] 停止 Streamlit UI...
taskkill /F /FI "WINDOWTITLE eq Streamlit UI*" >nul 2>&1
taskkill /F /IM streamlit.exe >nul 2>&1
echo   ✓ Streamlit UI 已停止

echo.
echo ========================================
echo 所有服务已停止
echo ========================================
echo.
pause
