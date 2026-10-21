@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Futu Multi-Tenant Stopper
echo.
echo  ============================================
echo    停止富途量化交易系统 (自适应多租户级联停止)
echo  ============================================
echo.

echo  [1/4] 停止所有活跃租户 Worker 与交易服务...
set "FOUND_WORKER=0"
for %%f in ("data\trading-service*.pid") do (
    set "FOUND_WORKER=1"
    set "SVC_PID="
    for /f "usebackq delims=" %%p in ("%%f") do set "SVC_PID=%%p"
    if defined SVC_PID (
        taskkill /PID !SVC_PID! /T /F >nul 2>&1
        echo    [?] 租户服务 Worker [%%~nxf] 已停止 PID=!SVC_PID!
    )
    del "%%f" >nul 2>&1
)
if "!FOUND_WORKER!"=="0" (
    taskkill /FI "WINDOWTITLE eq TradingService*" /T /F >nul 2>&1
    echo    [i] 未发现存活租户 PID 文件，已按窗口标题执行清理
)
del "data\trading-service*.stop" >nul 2>&1
del "data\trading-service*.restart" >nul 2>&1

echo.
echo  [2/4] 停止波动率雷达...
set "RADAR_PID="
if exist "data\volatility-radar.pid" for /f "usebackq delims=" %%r in ("data\volatility-radar.pid") do set "RADAR_PID=%%r"
if defined RADAR_PID (
    taskkill /PID %RADAR_PID% /T /F >nul 2>&1
    echo    [?] 波动率雷达已停止 PID=%RADAR_PID%
)
del "data\volatility-radar.pid" >nul 2>&1
del "data\volatility-radar.stop" >nul 2>&1

echo.
echo  [3/4] 停止 Streamlit UI...
taskkill /FI "WINDOWTITLE eq StreamlitUI*" /T /F >nul 2>&1
taskkill /IM streamlit.exe /T /F >nul 2>&1
echo    [?] Streamlit 界面已安全退出

echo.
echo  [4/4] 清理完成
echo.
echo  ============================================
echo    所有服务与租户 Worker 已全量安全停止
echo  ============================================
echo.
pause