[CmdletBinding()]
param(
    [string]$TaskName = "TradeFuto-ML-Monthly-Retrain",
    [string]$At = "21:00"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_ml_retrain_monthly.ps1"
$localZone = (tzutil /g).Trim()
# Windows PowerShell 5.1 的 New-ScheduledTaskTrigger 不支持 -Monthly；
# 使用系统自带 schtasks.exe 注册每月第 1 日任务，兼容 Win10/Win11。
$taskRun = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $Runner + '"'
$time = ([datetime]::ParseExact($At, "HH:mm", $null)).ToString("HH:mm")
& schtasks.exe /Create /TN $TaskName /TR $taskRun /SC MONTHLY /D 1 /ST $time /F /RL LIMITED /RU $env:USERNAME
if ($LASTEXITCODE -ne 0) {
    throw "schtasks 注册失败，退出码：$LASTEXITCODE"
}
Write-Output "已注册计划任务：$TaskName"
Write-Output "执行时间：每月1日 $At（本机时区：$localZone）"
Write-Output "执行脚本：$Runner"
Write-Output "注意：若需电脑错过后补执行或高级重试策略，请在任务计划程序中打开该任务的设置页配置。"
