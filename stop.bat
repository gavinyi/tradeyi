@echo off
setlocal EnableExtensions
title Futu Paper Quant Stopper
echo.
echo  ============================================
echo    停止富途量化交易系统
echo  ============================================
echo.

echo  [1/4] 停止交易服务...
set "SVC_PID="
if exist "data\trading-service.pid" for /f "usebackq delims=" %%p in ("data\trading-service.pid") do set "SVC_PID=%%p"
if defined SVC_PID (
    taskkill /PID %SVC_PID% /T /F >nul 2>&1
    echo    交易服务已停止 PID=%SVC_PID%
) else (
    taskkill /FI "WINDOWTITLE eq TradingService*" /T /F >nul 2>&1
    echo    未找到交易服务 PID，已按窗口标题清理
)
del "data\trading-service.pid" >nul 2>&1
del "data\trading-service.stop" >nul 2>&1
del "data\trading-service.restart" >nul 2>&1

echo  [2/4] 停止波动率雷达...
set "RADAR_PID="
if exist "data\volatility-radar.pid" for /f "usebackq delims=" %%r in ("data\volatility-radar.pid") do set "RADAR_PID=%%r"
if defined RADAR_PID (
    taskkill /PID %RADAR_PID% /T /F >nul 2>&1
    echo    波动率雷达已停止 PID=%RADAR_PID%
)
del "data\volatility-radar.pid" >nul 2>&1
del "data\volatility-radar.stop" >nul 2>&1

echo  [3/4] 停止 Streamlit UI...
taskkill /FI "WINDOWTITLE eq StreamlitUI*" /T /F >nul 2>&1
taskkill /IM streamlit.exe /T /F >nul 2>&1

echo  [4/4] 清理完成
echo.
echo  ============================================
echo    所有服务已停止
echo  ============================================
echo.
pause
