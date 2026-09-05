@echo off
REM 富途量化交易系统 - Windows 启动脚本
REM 使用方法：双击运行此文件

echo ========================================
echo 富途量化交易系统启动
echo ========================================
echo.

REM 检查虚拟环境
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境，请先运行 deploy-windows.ps1
    pause
    exit /b 1
)

REM 检查富途 OpenD
echo [1/3] 检查富途 OpenD 连接...
.venv\Scripts\python.exe -c "from futu import OpenQuoteContext; ctx = OpenQuoteContext('127.0.0.1', 11111); result = ctx.start(); print('连接成功' if result == 0 else '连接失败'); ctx.close()" 2>nul
if errorlevel 1 (
    echo [警告] 富途 OpenD 未启动或连接失败
    echo 请确保：
    echo   1. 富途牛牛客户端已登录
    echo   2. OpenD 服务已启动
    echo.
    choice /M "是否继续启动服务"
    if errorlevel 2 exit /b 1
)

echo [2/3] 启动交易服务...
REM 启动交易服务（最小化窗口）
start "Trading Service" /MIN cmd /c ".venv\Scripts\python.exe scripts\run_service.py > data\service.log 2>&1"

REM 等待服务启动
timeout /t 3 /nobreak >nul

REM 检查服务是否启动成功
if exist "data\trading-service.pid" (
    set /p PID=<data\trading-service.pid
    echo   ✓ 交易服务已启动 (PID: %PID%)
) else (
    echo   ✗ 交易服务启动失败，请检查日志
)

echo [3/3] 启动 Streamlit UI...
REM 启动 Streamlit UI
start "Streamlit UI" cmd /c ".venv\Scripts\streamlit.exe run app.py --server.port 8501"

REM 等待 UI 启动
timeout /t 3 /nobreak >nul

echo.
echo ========================================
echo 启动完成！
echo ========================================
echo.
echo 服务状态：
echo   • 交易服务: 运行中
echo   • Streamlit UI: http://localhost:8501
echo.
echo 日志位置：
echo   • 服务日志: data\service.log
echo   • PID 文件: data\trading-service.pid
echo.
echo 按任意键打开浏览器...
pause >nul

REM 打开浏览器
start http://localhost:8501
