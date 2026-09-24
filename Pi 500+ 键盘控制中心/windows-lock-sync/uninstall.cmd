@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Unregister-ScheduledTask -TaskName 'Pi500 Lock Sync - Locked' -Confirm:$false -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName 'Pi500 Lock Sync - Unlocked' -Confirm:$false -ErrorAction SilentlyContinue; Remove-Item (Join-Path $env:LOCALAPPDATA 'Pi500LockSync') -Recurse -Force -ErrorAction SilentlyContinue; Write-Host '已卸载 Windows 锁屏同步。'"
pause
