@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "ELEVATED=%~1"

rem ================================================================
rem 条件提权：普通权限先杀一遍；只有确有 Worker 存活时才自提权重试。
rem 这样正常情况下双击即停、零 UAC 弹窗；仅管理员权限启动的 Worker 才会触发提权。
rem ================================================================

title Futu Multi-Tenant Stopper
echo.
echo  ============================================
echo    停止富途量化交易系统 (自适应多租户级联停止)
echo  ============================================
echo.
if "!ELEVATED!"=="elevated" (
    echo  （管理员权限重试）
    echo.
)
echo  本脚本逐个停止所有处于运行状态的租户 Worker，每一个都会复查；
echo  任一未停掉将红字报错并退出码 1，绝不静默失败。
echo.
rem P1-13 维护标记：写本文件期间，守护脚本（scripts\scheduled_log_audit.py）静默，
rem 避免把正常启停误报成进程暴毙。start.bat 启动时删除；本脚本失败退出路径也删除，
rem 否则一次失败停止就会永久静默。
echo %DATE% %TIME% > "data\.maintenance"

echo  [1/4] 停止所有活跃租户 Worker 与交易服务...
set "FOUND_WORKER=0"
set "LEFT_OVER="
for %%f in ("data\trading-service*.pid") do (
    set "FOUND_WORKER=1"
    set "SVC_PID="
    set /p "SVC_PID=" < "%%f"
    if defined SVC_PID (
        set "SVC_PID=!SVC_PID: =!"
        call :stop_pid !SVC_PID! "%%~nxf" "run_service"
    ) else (
        echo    [SKIP] PID 文件为空 [%%~nxf]
    )
    call :del_file "%%f"
)
if "!FOUND_WORKER!"=="0" (
    taskkill /FI "WINDOWTITLE eq TradingService*" /T /F >nul 2>&1
    echo    [i] 未发现存活租户 PID 文件，已按窗口标题执行清理
)
del "data\trading-service*.stop" >nul 2>&1
del "data\trading-service*.restart" >nul 2>&1

rem ================================================================
rem 条件提权判定：普通权限没杀干净 -> 提权重试一次。
rem ================================================================
if defined LEFT_OVER if not "!ELEVATED!"=="elevated" (
    echo.
    echo  ============================================
    echo    普通权限无法停止以下 Worker：!LEFT_OVER!
    echo    这些进程可能是以管理员权限启动的。
    echo    即将请求管理员权限后重试（会弹出一次 UAC，请点「是」）。
    echo  ============================================
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList 'elevated' -Verb RunAs"
    rem P1-13: 提权重试退出前必须清标记 —— 此刻 Worker 可能仍在运行，
    rem 标记留着会让守护一直静默（暴毙/心跳超时都不告警），比不告警更糟。
    del "data\.maintenance" >nul 2>&1
    rem 提权副本启动时会自己重写标记，所以这里删掉不丢「维护中」语义。
    exit /b 1
)

echo.
echo  [2/4] 停止波动率雷达...
set "RADAR_PID="
if exist "data\volatility-radar.pid" set /p "RADAR_PID=" < "data\volatility-radar.pid"
if defined RADAR_PID (
    set "RADAR_PID=%RADAR_PID: =%"
    call :stop_pid %RADAR_PID% "volatility-radar" "run_radar_service"
)
call :del_file "data\volatility-radar.pid"
del "data\volatility-radar.stop" >nul 2>&1

echo.
echo  [3/4] 停止 Streamlit UI...
taskkill /FI "WINDOWTITLE eq StreamlitUI*" /T /F >nul 2>&1
taskkill /IM streamlit.exe /T /F >nul 2>&1
echo    [?] Streamlit 界面已安全退出

echo.
echo  [4/4] 全量自检：确认所有 Worker 已真正停止...
set "STILL_ALIVE="
if defined LEFT_OVER (
    for %%p in (!LEFT_OVER!) do (
        call :is_alive %%p
        if defined IS_ALIVE set "STILL_ALIVE=!STILL_ALIVE! %%p"
    )
)
set "PIDFILE_LEFT="
for %%f in ("data\trading-service*.pid") do set "PIDFILE_LEFT=1"
if defined PIDFILE_LEFT set "STILL_ALIVE=!STILL_ALIVE! (残留PID文件)"
if defined STILL_ALIVE (
    echo.
    echo  ============================================
    echo    [X] 有 Worker 未能停止：!STILL_ALIVE!
    echo        请手工执行 taskkill /PID ^<PID^> /T /F
    echo        确认全部停止后再运行 start.bat
    echo  ============================================
    echo.
    pause
    rem P1-13: 停止失败必须清标记，否则守护会一直静默（看不见异常）
    del "data\.maintenance" >nul 2>&1
    exit /b 1
)
echo    [OK] 所有租户 Worker 已确认停止
echo.
echo  ============================================
echo    所有服务与租户 Worker 已全量安全停止
echo  ============================================
echo.
pause
goto :eof

rem ================================================================
rem :stop_pid - 验身份后杀掉并复查。%1=PID  %2=显示名  %3=命令行标记(脚本名)
rem 杀之前必须验身份：PID 会被系统回收复用，只凭 PID 文件就 taskkill 会杀掉无关进程。
rem ================================================================
:stop_pid
set "SP_PID=%~1"
set "SP_NAME=%~2"
set "SP_MARK=%~3"
set "SP_LEFT="
if not defined SP_PID goto :eof
call :pid_identity_ok %SP_PID% "%SP_MARK%"
if defined IDENTITY_FOREIGN (
    echo    [SKIP] PID=%SP_PID% 不是我们的进程（PID 已被系统复用），已跳过，绝不误杀 [%SP_NAME%]
    goto :eof
)
if not defined IDENTITY_OK (
    if not defined PI_IMG (
        echo    [OK] %SP_NAME% 已停止 PID=%SP_PID% （进程不存在）
        goto :eof
    )
    echo    [WARN] PID=%SP_PID% 身份无法确认（期望命令行含 %SP_MARK%），不盲杀，交自检处理 [%SP_NAME%]
    set "SP_LEFT=1"
    set "LEFT_OVER=%LEFT_OVER% %SP_PID%"
    goto :eof
)
taskkill /PID %SP_PID% /T /F >nul 2>&1
ping -n 2 127.0.0.1 >nul 2>&1
call :is_alive %SP_PID% "%SP_MARK%"
if not defined IS_ALIVE (
    echo    [OK] %SP_NAME% 已停止 PID=%SP_PID%
    goto :eof
)
rem 第一次没杀掉：放开输出再试一次，让错误原文可见
taskkill /PID %SP_PID% /T /F
ping -n 2 127.0.0.1 >nul 2>&1
call :is_alive %SP_PID% "%SP_MARK%"
if defined IS_ALIVE (
    set "SP_LEFT=1"
    echo    [FAILED] PID=%SP_PID% 仍在运行 [%SP_NAME%]
) else (
    echo    [OK] %SP_NAME% 已停止 PID=%SP_PID% （第二次尝试成功）
)
if defined SP_LEFT set "LEFT_OVER=%LEFT_OVER% %SP_PID%"
goto :eof
rem ================================================================
rem :del_file - 删除文件，失败重试 5 次；仍失败打印 [FAILED]。
rem ================================================================
:del_file
set "DF_FILE=%~1"
if not exist "%DF_FILE%" goto :eof
set /a "DF_N=0"
:del_retry
del "%DF_FILE%" >nul 2>&1
if not exist "%DF_FILE%" goto :eof
set /a "DF_N+=1"
if !DF_N! geq 5 (
    echo    [FAILED] 无法删除 %DF_FILE% （文件仍被占用）
    goto :eof
)
ping -n 2 127.0.0.1 >nul 2>&1
goto :del_retry

rem ================================================================
rem :is_alive - 只读复查（含身份校验）。%1=PID  %2=命令行标记
rem PID 复用时会得出错误结论：把"没杀掉"报成"已停止"（静默失败），所以必须验身份。
rem ================================================================
:is_alive
set "IA_PID=%~1"
set "IA_MARK=%~2"
set "IS_ALIVE="
if not defined IA_PID goto :eof
call :pid_identity_ok %IA_PID% "%IA_MARK%"
if defined IDENTITY_OK set "IS_ALIVE=1"
goto :eof

rem ================================================================
rem :pid_identity_ok - 判定 PID 是否属于我们的 python 进程。
rem   %1=PID  %2=期望的命令行标记（脚本名；留空则只校验映像名）
rem 置位：IDENTITY_OK=1 是我们的；IDENTITY_FOREIGN=1 存在但不是我们的（PID 复用）。
rem 两者都未置位：进程不存在，或存在且是 python 但命令行读不到/标记不符（存疑，不盲杀）。
rem 与 start.bat 的 :pid_alive、launch_workers.is_pid_alive 同一口径。
rem ================================================================
:pid_identity_ok
set "IDENTITY_OK="
set "IDENTITY_FOREIGN="
set "PI_IMG="
if "%~1"=="" goto :eof
for /f "skip=1 tokens=1 delims=," %%A in ('tasklist /FI "PID eq %~1" /FO CSV 2^>nul') do set "PI_IMG=%%~A"
if not defined PI_IMG goto :eof
echo %PI_IMG%|findstr /R /C:"^python">nul
if errorlevel 1 (
    set "IDENTITY_FOREIGN=1"
    goto :eof
)
if "%~2"=="" (
    set "IDENTITY_OK=1"
    goto :eof
)
powershell -NoProfile -Command "if((Get-CimInstance Win32_Process -Filter ('ProcessId='+%~1) -ErrorAction SilentlyContinue).CommandLine -match '%~2'){exit 0};exit 1">nul 2>&1
if errorlevel 1 goto :eof
set "IDENTITY_OK=1"
goto :eof
