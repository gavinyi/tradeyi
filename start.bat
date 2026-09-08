@echo off
setlocal EnableExtensions
title Futu Paper Quant Launcher
rem ============================================================
rem  富途量化交易系统 - Windows 启动脚本
rem  用法：双击运行，或在 cmd/PowerShell 中执行 start.bat
rem ============================================================
echo.
echo  ============================================
echo    富途量化交易系统启动
echo  ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo  [错误] 未找到虚拟环境，请先运行 deploy-windows.ps1
    pause
    exit /b 1
)

rem ---- [1/4] 检查富途 OpenD ----
echo  [1/4] 检查富途 OpenD 连接...
.venv\Scripts\python.exe -c "from futu import OpenQuoteContext; from futu.common.constant import ContextStatus; ctx=OpenQuoteContext('127.0.0.1',11111); ok=ctx.status in (ContextStatus.CONNECTED,ContextStatus.READY); print('连接成功' if ok else '连接失败'); ctx.close(); raise SystemExit(0 if ok else 1)" 2>nul
if errorlevel 1 (
    echo  [警告] 无法连接富途 OpenD。
    echo     1. 请确认富途牛牛客户端已登录
    echo     2. 请确认 OpenD 服务已启动（端口 11111）
    choice /C YN /M "是否继续启动服务"
    if errorlevel 2 exit /b 1
)

rem ---- [2/4] 启动交易服务 ----
echo  [2/4] 启动交易服务...
start "TradingService" /MIN cmd /c "cd /d %~dp0 && .venv\Scripts\python.exe scripts\run_service.py >> data\service.log 2>&1"

set "SVC_PID="
set /a WAIT=0
:wait_svc
set "SVC_PID="
if exist "data\trading-service.pid" for /f "usebackq delims=" %%p in ("data\trading-service.pid") do set "SVC_PID=%%p"
if defined SVC_PID goto svc_ok
set /a WAIT+=1
if %WAIT% GEQ 20 goto svc_fail
timeout /t 1 /nobreak >nul
goto wait_svc
:svc_ok
echo    交易服务已启动 PID=%SVC_PID%
goto svc_done
:svc_fail
echo    [警告] 交易服务 20 秒内未就绪，请查看 data\service.log
:svc_done

rem ---- [3/4] 确认波动率雷达（交易服务会自动拉起）----
timeout /t 2 /nobreak >nul
set "RADAR_PID="
if exist "data\volatility-radar.pid" for /f "usebackq delims=" %%r in ("data\volatility-radar.pid") do set "RADAR_PID=%%r"
if defined RADAR_PID (
    echo    波动率雷达已启动 PID=%RADAR_PID%
) else (
    echo    [提示] 波动率雷达尚未就绪（交易服务会自动启动它）
)

rem ---- [4/4] 启动 Streamlit UI ----
echo  [4/4] 启动 Streamlit UI...
start "StreamlitUI" /MIN cmd /c "cd /d %~dp0 && .venv\Scripts\streamlit.exe run app.py --server.port 8501 >> data\streamlit.log 2>&1"
timeout /t 5 /nobreak >nul

echo.
echo  ============================================
echo    启动完成
echo  ============================================
echo.
echo   Web 界面:  http://localhost:8501
echo   服务日志:  data\service.log
echo   雷达日志:  data\volatility-radar.log
echo.
echo   按任意键打开浏览器...
pause >nul
start http://localhost:8501
