@echo off
setlocal EnableExtensions EnableDelayedExpansion
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
call :pid_alive "%SERVICE_PID%"
if "!PID_ALIVE!"=="1" goto service_done
del /q "%SERVICE_PID%" 2>nul
echo [2/3] Starting Trading Service with project Python ...
start "TradeFuto Trading Service" /b "%PY%" scripts\run_service.py >> "%SERVICE_LOG%" 2>&1
:service_done
call :pid_alive "%RADAR_PID%"
if "!PID_ALIVE!"=="1" goto radar_done
del /q "%RADAR_PID%" 2>nul
echo [3/3] Starting independent Volatility Radar with project Python ...
start "TradeFuto Volatility Radar" /b "%PY%" scripts\run_radar_service.py >> "%RADAR_LOG%" 2>&1
:radar_done
echo TradeFuto started with the project .venv.
echo UI: http://127.0.0.1:8501
exit /b 0

:pid_alive
set "PID_ALIVE=0"
if not exist "%~1" exit /b 0
set /p CHECK_PID=<"%~1"
if not defined CHECK_PID exit /b 0
tasklist /FI "PID eq %CHECK_PID%" 2>nul | findstr /R /C:" %CHECK_PID% " >nul
if errorlevel 1 exit /b 0
set "PID_ALIVE=1"
exit /b 0