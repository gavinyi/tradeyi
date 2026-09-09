[CmdletBinding()]
param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $Root "data\ml_training\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogPath = Join-Path $LogDir "ml_retrain_$stamp.log"

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    Write-Output $line
}

try {
    Push-Location $Root
    $env:PYTHONUTF8 = "1"
    if (-not $PythonPath) {
        $PythonPath = Join-Path $Root ".venv\Scripts\python.exe"
    }
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        throw "找不到项目虚拟环境 Python：$PythonPath"
    }

    Write-Log "开始月度 ML 重训练，工作目录：$Root"
    Write-Log "使用项目虚拟环境 Python：$PythonPath"
    & $PythonPath -u scripts/ml_retrain_monthly.py `
        --model-output (Join-Path $Root "data\ml_models\volatility_scorer_v3.pkl") 2>&1 | ForEach-Object {
        Add-Content -LiteralPath $LogPath -Value ([string]$_) -Encoding UTF8
        Write-Output $_
    }
    $exitCode = $LASTEXITCODE
    Write-Log "月度 ML 重训练结束，退出码：$exitCode"

    Get-ChildItem -LiteralPath $LogDir -Filter "ml_retrain_*.log" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 12 |
        Remove-Item -Force -ErrorAction SilentlyContinue
    exit $exitCode
} catch {
    Write-Log "月度 ML 重训练失败：$($_.Exception.Message)"
    exit 1
} finally {
    Pop-Location
}
