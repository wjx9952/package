param(
    [Parameter(Mandatory = $true)][string]$PiAddress,
    [Parameter(Mandatory = $true)][ValidateSet("Locked", "Unlocked")][string]$State
)

$statePath = $State.ToLowerInvariant()
$uri = "http://${PiAddress}:8765/session/windows/${statePath}"

try {
    Invoke-RestMethod -Uri $uri -Method Post -TimeoutSec 3 | Out-Null
} catch {
    # The next lock/unlock or logon event will retry automatically.
}
