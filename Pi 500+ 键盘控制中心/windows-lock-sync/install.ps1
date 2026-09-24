param([string]$PiAddress = "")

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($PiAddress)) {
    $PiAddress = Read-Host "请输入树莓派 IP 地址（小屏幕 IP 后面的地址）"
}
$PiAddress = $PiAddress.Trim()
if ([string]::IsNullOrWhiteSpace($PiAddress)) {
    throw "树莓派 IP 地址不能为空。"
}

$statusUri = "http://${PiAddress}:8765/session/windows"
try {
    $status = Invoke-RestMethod -Uri $statusUri -Method Get -TimeoutSec 5
    if (-not $status.ok) { throw "树莓派返回异常状态。" }
} catch {
    throw "无法连接树莓派 ${PiAddress}:8765。请确认两台设备在同一网络且树莓派程序已更新。"
}

$installDir = Join-Path $env:LOCALAPPDATA "Pi500LockSync"
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
$clientPath = Join-Path $installDir "Pi500LockSync.ps1"
Copy-Item (Join-Path $PSScriptRoot "Pi500LockSync.ps1") $clientPath -Force

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$escapedUser = [System.Security.SecurityElement]::Escape($userId)
$escapedClient = [System.Security.SecurityElement]::Escape($clientPath)
$escapedPi = [System.Security.SecurityElement]::Escape($PiAddress)

function New-LockSyncTaskXml {
    param(
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][bool]$IncludeLogon
    )

    $logonTrigger = ""
    if ($IncludeLogon) {
        $logonTrigger = @"
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$escapedUser</UserId>
    </LogonTrigger>
"@
    }
    $sessionChange = if ($State -eq "Locked") { "SessionLock" } else { "SessionUnlock" }
    $arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File &quot;$escapedClient&quot; -PiAddress &quot;$escapedPi&quot; -State $State"

    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>同步 Windows 锁屏状态到 Pi500 小屏幕</Description></RegistrationInfo>
  <Triggers>
    <SessionStateChangeTrigger>
      <Enabled>true</Enabled>
      <StateChange>$sessionChange</StateChange>
      <UserId>$escapedUser</UserId>
    </SessionStateChangeTrigger>
$logonTrigger  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$escapedUser</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT1M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$arguments</Arguments>
    </Exec>
  </Actions>
</Task>
"@
}

Register-ScheduledTask -TaskName "Pi500 Lock Sync - Locked" `
    -Xml (New-LockSyncTaskXml -State "Locked" -IncludeLogon $false) -Force | Out-Null
Register-ScheduledTask -TaskName "Pi500 Lock Sync - Unlocked" `
    -Xml (New-LockSyncTaskXml -State "Unlocked" -IncludeLogon $true) -Force | Out-Null

& $clientPath -PiAddress $PiAddress -State Unlocked
Write-Host ""
Write-Host "安装完成。Windows 锁定和解锁状态将自动同步到树莓派。" -ForegroundColor Green
Write-Host "已创建计划任务：Pi500 Lock Sync - Locked / Unlocked"
