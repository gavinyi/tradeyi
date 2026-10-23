@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem Unbuffered stdout: without this, Python block-buffers stdout when it is a file
rem (8KB), so a running-but-silent worker/radar leaves its log frozen for minutes
rem and "is it alive?" becomes unanswerable. -u is already passed below, but the
rem env var also covers any process the worker re-execs.
set "PYTHONUNBUFFERED=1"
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "STREAMLIT=%ROOT%.venv\Scripts\streamlit.exe"
set "DATA=%ROOT%data"
set "SERVICE_PID=%DATA%\trading-service.pid"
set "SERVICE_LOG=%DATA%\service.log"
set "UI_LOG=%DATA%\streamlit.log"
set "RADAR_PID=%DATA%\volatility-radar.pid"
set "RADAR_LOG=%DATA%\volatility-radar.log"
if not exist "%PY%" (echo ERROR: project Python not found & exit /b 1)
if not exist "%STREAMLIT%" (echo ERROR: project Streamlit not found & exit /b 1)
if not exist "%DATA%" mkdir "%DATA%"
cd /d "%ROOT%"
echo [1/3] Checking OpenD at 127.0.0.1:11111 ...
netstat -ano | findstr /R /C:":11111 .*LISTENING" >nul
if errorlevel 1 (echo OpenD is not listening on 127.0.0.1:11111 & exit /b 2)
netstat -ano | findstr /R /C:":8501 .*LISTENING" >nul
if errorlevel 1 start "TradeFuto Streamlit UI" /b "%STREAMLIT%" run app.py --server.port 8501
echo [2/3] Launching Adaptive Active Tenant Workers ...
"%PY%" -u scripts\launch_workers.py --py "%PY%"
:service_done
call :pid_alive "%RADAR_PID%" "run_radar_service"
if "!PID_ALIVE!"=="1" goto radar_done
del /q "%RADAR_PID%" 2>nul
echo [3/3] Starting independent Volatility Radar with project Python ...
rem Route the radar's stdout to the SAME file its Futu SDK handler writes to
rem (data/futu-logs/futu-radar.log, via FUTU_LOG_ROLE=radar). Without this the
rem SDK log and the console output land in two different files, and "where did
rem the radar log go" becomes unanswerable. The directory is created here
rem because the batch redirect opens the file before the radar's own handler
rem exists.
rem 雷达 stdout 必须与 Futu SDK 日志**分成两个文件**：SDK 的 file_logger 打开
rem data/futu-logs/futu-radar.log（broker.py:72-80），而这里若也重定向到同一个文件，
rem cmd 会先占住写句柄 → 雷达首次连接 OpenD 时 SDK 打日志被拒（Errno 13 Permission
rem denied），整轮扫描失败并退避。2026-10-23 21:36 实盘开盘首轮就是这个形态。
if not exist "%DATA%\futu-logs" mkdir "%DATA%\futu-logs"
start "TradeFuto Volatility Radar" /b "%PY%" -u scripts\run_radar_service.py >> "%DATA%\futu-logs\futu-radar-stdout.log" 2>&1
:radar_done
echo TradeFuto started with the project .venv.
echo UI: http://127.0.0.1:8501
exit /b 0

:pid_alive
rem %1 = pid file, %2 = expected script name.
rem A bare "does this PID exist" check is wrong: Windows recycles PIDs, so a
rem stale pid file can name an unrelated system process (tonight 8208 was
rem reused by csrss.exe) and the service would be silently skipped. Require
rem the PID to belong to one of our own Python processes: image name must be
rem python/pythonw, and the command line must name the expected script. The
rem marker is the script name, not the project path: our processes are
rem launched with relative paths, so the path is in the working directory,
rem not in the command line.
set "PID_ALIVE=0"
if not exist "%~1" exit /b 0
set "CHECK_PID="
for /f "usebackq delims=" %%P in ("%~1") do set "CHECK_PID=%%P"
if not defined CHECK_PID exit /b 0
if "%~2"=="" exit /b 0
set "IMG="
for /f "skip=1 tokens=1 delims=," %%A in ('tasklist /FI "PID eq %CHECK_PID%" /FO CSV 2^>nul') do set "IMG=%%~A"
if not defined IMG exit /b 0
echo %IMG%|findstr /R /C:"^python">nul
if errorlevel 1 exit /b 0
powershell -NoProfile -Command "if((Get-CimInstance Win32_Process -Filter ('ProcessId='+%CHECK_PID%) -ErrorAction SilentlyContinue).CommandLine -match '%~2'){exit 0};exit 1">nul 2>&1
if errorlevel 1 exit /b 0
set "PID_ALIVE=1"
exit /b 0