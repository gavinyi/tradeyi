[CmdletBinding()]
param(
    [string]$TaskName = "TradeFuto-ML-Monthly-Retrain",
    [string]$At = "21:00"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_ml_retrain_monthly.ps1"
$localZone = (tzutil /g).Trim()
$actionArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $Runner + '"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $actionArgs
$triggerTime = [datetime]::ParseExact($At, "HH:mm", $null)
$trigger = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 1 -At $triggerTime
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType InteractiveToken -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "每月1日执行 ML 波动率标的筛选模型重训练；AUC 安全门控后才替换生产模型。" -Force | Out-Null
Write-Output "已注册计划任务：$TaskName"
Write-Output "执行时间：每月1日 $At（本机时区：$localZone）"
Write-Output "执行脚本：$Runner"
