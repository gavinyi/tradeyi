# 富途量化交易系统 - Windows 部署脚本
# 使用方法：在 PowerShell 中运行 .\deploy-windows.ps1

$ErrorActionPreference = "Stop"

# Unbuffered stdout: Python block-buffers stdout when it is a file (8KB), which
# freezes service/radar logs and makes a running process invisible. Set it for the
# whole script so every child process inherits it.
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "富途量化交易系统 - Windows 部署" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 1. 检查 Python
Write-Host "[1/7] 检查 Python 环境..." -ForegroundColor Yellow
if (!(Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "  ✗ 错误：未找到 Python" -ForegroundColor Red
    Write-Host "  请先安装 Python 3.11+: https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}

$pythonVersion = python --version
Write-Host "  ✓ $pythonVersion" -ForegroundColor Green

# 2. 检查 uv
Write-Host "[2/7] 检查包管理器 uv..." -ForegroundColor Yellow
if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "  未找到 uv，正在安装..." -ForegroundColor Yellow
    try {
        pip install uv
        Write-Host "  ✓ uv 安装成功" -ForegroundColor Green
    } catch {
        Write-Host "  ✗ uv 安装失败：$_" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  ✓ uv 已安装" -ForegroundColor Green
}

# 3. 创建虚拟环境并安装依赖
Write-Host "[3/7] 安装项目依赖..." -ForegroundColor Yellow
Write-Host "  这可能需要几分钟，请耐心等待..." -ForegroundColor Gray

$syncLog = Join-Path $PSScriptRoot "data\uv-sync.log"
try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $syncLog) | Out-Null
    & cmd.exe /c "uv sync > `"$syncLog`" 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ✗ 依赖安装失败（uv 退出码：$LASTEXITCODE）" -ForegroundColor Red
        Write-Host "  完整日志已保存：$syncLog" -ForegroundColor Yellow
        if (Test-Path $syncLog) {
            Write-Host "  --- 日志末尾（最后 25 行）---" -ForegroundColor Gray
            Get-Content $syncLog -Tail 25 | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }
            Write-Host "  -------------------------------" -ForegroundColor Gray
        }
        Write-Host "  提示：如果看到编译错误，请安装 Visual Studio Build Tools" -ForegroundColor Yellow
        Write-Host "  提示：如果看到缓存/权限错误，先执行 uv cache clean 再重试" -ForegroundColor Yellow
        exit 1
    }
    if (Test-Path $syncLog) { Remove-Item $syncLog -ErrorAction SilentlyContinue }
    Write-Host "  ✓ 依赖安装成功" -ForegroundColor Green
} catch {
    Write-Host "  ✗ 依赖安装失败：$_" -ForegroundColor Red
    Write-Host "  提示：如果遇到编译错误，请安装 Visual Studio Build Tools" -ForegroundColor Yellow
    exit 1
}

# 4. 检查数据库
Write-Host "[4/7] 检查数据库文件..." -ForegroundColor Yellow
if (!(Test-Path "data\trading.db")) {
    Write-Host "  ⚠ 警告：数据库文件不存在" -ForegroundColor Yellow
    Write-Host "  首次运行时将自动创建数据库" -ForegroundColor Gray
} else {
    $dbSize = (Get-Item "data\trading.db").Length / 1MB
    Write-Host "  ✓ 数据库文件存在 ($([math]::Round($dbSize, 2)) MB)" -ForegroundColor Green
}

# 5. 检查配置文件
Write-Host "[5/7] 检查配置文件..." -ForegroundColor Yellow
if (!(Test-Path ".env")) {
    Write-Host "  ⚠ 警告：.env 文件不存在" -ForegroundColor Yellow
    
    if (Test-Path ".env.example") {
        Write-Host "  正在从 .env.example 创建配置..." -ForegroundColor Gray
        Copy-Item ".env.example" ".env"
        Write-Host "  ✓ 已创建 .env 文件，请编辑配置" -ForegroundColor Green
        Write-Host "  编辑命令：notepad .env" -ForegroundColor Cyan
    } else {
        Write-Host "  请手动创建 .env 文件" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  ✓ .env 文件存在" -ForegroundColor Green
}

# 6. 测试富途连接
Write-Host "[6/7] 测试富途 OpenD 连接..." -ForegroundColor Yellow
# Sink APPDATA before importing futu: the SDK's FTLog writes to
# %APPDATA%\com.futunn.FutuOpenD\Log at import time, and on a sandboxed or
# locked-down machine that path is not writable (the import then fails with a
# bare PermissionError). Every other entry point does this via
# futu_quant.broker:15-18; this inline probe must do it itself because it
# imports futu directly.
$testScript = @"
import os
from pathlib import Path
_appdata = Path(r'$PSScriptRoot\data\appdata')
_appdata.mkdir(parents=True, exist_ok=True)
os.environ['APPDATA'] = str(_appdata)
from futu import OpenQuoteContext
try:
    from futu.common.constant import ContextStatus
except ImportError:
    ContextStatus = None
try:
    ctx = OpenQuoteContext('127.0.0.1', 11111)
    if ContextStatus is not None:
        ok = getattr(ctx, 'status', None) in (ContextStatus.CONNECTED, ContextStatus.READY)
    else:
        ok = ctx.start() == 0
    ctx.close()
    exit(0 if ok else 1)
except Exception as e:
    print(f'连接失败: {e}')
    exit(1)
"@

$testResult = & .venv\Scripts\python.exe -c $testScript 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ 富途 OpenD 连接成功" -ForegroundColor Green
} else {
    Write-Host "  ⚠ 警告：无法连接到富途 OpenD" -ForegroundColor Yellow
    Write-Host "  请确保：" -ForegroundColor Gray
    Write-Host "    1. 富途牛牛客户端已登录" -ForegroundColor Gray
    Write-Host "    2. OpenD 服务已启动（端口 11111）" -ForegroundColor Gray
    Write-Host "  提示：$testResult" -ForegroundColor Gray
}

# 7. 测试运行
Write-Host "[7/7] 测试服务运行..." -ForegroundColor Yellow
$testLog = Join-Path $PSScriptRoot "data\service-once.log"
try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $testLog) | Out-Null
    & cmd.exe /c ".venv\Scripts\python.exe scripts\run_service.py --once > `"$testLog`" 2>&1"
    if ($LASTEXITCODE -eq 0) {
        if (Test-Path $testLog) { Remove-Item $testLog -ErrorAction SilentlyContinue }
        Write-Host "  ✓ 服务测试成功" -ForegroundColor Green
    } else {
        Write-Host "  ⚠ 警告：服务测试返回非零代码（完整输出见 $testLog）" -ForegroundColor Yellow
        if (Test-Path $testLog) {
            Write-Host "  --- 输出末尾（最后 20 行）---" -ForegroundColor Gray
            Get-Content $testLog -Tail 20 | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }
            Write-Host "  -----------------------------" -ForegroundColor Gray
        }
    }
} catch {
    Write-Host "  ✗ 服务测试失败：$_" -ForegroundColor Red
    if (Test-Path $testLog) { Write-Host "  完整输出见 $testLog" -ForegroundColor Yellow }
    exit 1
}

# 部署完成
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "部署完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "下一步操作：" -ForegroundColor Yellow
Write-Host ""
Write-Host "1. 编辑配置文件（如需要）：" -ForegroundColor White
Write-Host "   notepad .env" -ForegroundColor Cyan
Write-Host ""
Write-Host "2. 启动系统（推荐）：" -ForegroundColor White
Write-Host "   双击运行 start.bat" -ForegroundColor Cyan
Write-Host ""
Write-Host "3. 或手动启动：" -ForegroundColor White
Write-Host "   启动服务：uv run python scripts\run_service.py" -ForegroundColor Cyan
Write-Host "   启动 UI： uv run streamlit run app.py" -ForegroundColor Cyan
Write-Host ""
Write-Host "4. 访问界面：" -ForegroundColor White
Write-Host "   http://localhost:8501" -ForegroundColor Cyan
Write-Host ""
Write-Host "查看完整文档：WINDOWS_MIGRATION.md" -ForegroundColor Gray
Write-Host ""
