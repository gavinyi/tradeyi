[CmdletBinding()]
param(
    [string]$TaskName = "TradeFuto-ML-Monthly-Retrain",
    [string]$At = "21:00"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Runner = Join-Path $Root "scripts\run_ml_retrain_monthly.ps1"
$localZone = (tzutil /g).Trim()
if (-not (Test-Path -LiteralPath $Runner)) { throw "找不到训练执行器：$Runner" }

$time = ([datetime]::ParseExact($At, "HH:mm", $null)).ToString("HH:mm:ss")
$startDate = (Get-Date).ToString("yyyy-MM-dd")
$userId = "$env:USERDOMAIN\$env:USERNAME"
$xmlPath = Join-Path $env:TEMP ("TradeFuto-ML-Monthly-Retrain-{0}.xml" -f ([guid]::NewGuid().ToString("N")))

$xmlLines = @(
    '<?xml version="1.0" encoding="UTF-16"?>'
    '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
    '  <RegistrationInfo>'
    '    <Description>每月1日执行 ML 波动率标的筛选模型重训练；AUC 安全门控后才替换生产模型。</Description>'
    ('    <URI>\{0}</URI>' -f $TaskName)
    '  </RegistrationInfo>'
    '  <Triggers><CalendarTrigger>'
    ('    <StartBoundary>{0}T{1}</StartBoundary>' -f $startDate, $time)
    '    <Enabled>true</Enabled><ScheduleByMonth>'
    '      <DaysOfMonth><Day>1</Day></DaysOfMonth>'
    '      <Months><January/><February/><March/><April/><May/><June/><July/><August/><September/><October/><November/><December/></Months>'
    '    </ScheduleByMonth></CalendarTrigger></Triggers>'
    '  <Principals><Principal id="Author">'
    ('    <UserId>{0}</UserId>' -f $userId)
    '    <LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>'
    '  </Principal></Principals>'
    '  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>'
    '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>'
    '    <StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT12H</ExecutionTimeLimit><AllowHardTerminate>true</AllowHardTerminate>'
    '  </Settings>'
    '  <Actions Context="Author"><Exec><Command>powershell.exe</Command>'
    ('    <Arguments>-NoProfile -ExecutionPolicy Bypass -File "{0}"</Arguments>' -f $Runner)
    ('    <WorkingDirectory>{0}</WorkingDirectory>' -f $Root)
    '  </Exec></Actions>'
    '</Task>'
)
$xml = $xmlLines -join "`r`n"

try {
    [System.IO.File]::WriteAllText($xmlPath, $xml, (New-Object System.Text.UnicodeEncoding($false, $true)))
    & schtasks.exe /Create /TN $TaskName /XML $xmlPath /F
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw "schtasks registration failed; exit code: $exitCode" }
} finally {
    Remove-Item -LiteralPath $xmlPath -Force -ErrorAction SilentlyContinue
}

Write-Output "已注册计划任务：$TaskName"
Write-Output "执行时间：每月1日 $At（本机时区：$localZone）"
Write-Output "执行脚本：$Runner"
