# Registers (or removes) the logon task that keeps the agent's scheduler running.
#
#   Install:  powershell -ExecutionPolicy Bypass -File scripts\install_scheduler_task.ps1
#   Remove:   powershell -ExecutionPolicy Bypass -File scripts\install_scheduler_task.ps1 -Uninstall
#
# A user-level task, not a service: it runs as you, with your environment and your
# .env credentials, and needs no elevation. Removing it is one command and leaves
# nothing behind.

param(
    [switch]$Uninstall,
    [string]$TaskName = 'FO Intelligence Agent scheduler'
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Runner   = Join-Path $RepoRoot 'scripts\run_scheduler.ps1'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "Removed scheduled task '$TaskName'."
    } else {
        "No scheduled task named '$TaskName' -- nothing to remove."
    }
    return
}

if (-not (Test-Path $Runner)) { throw "Runner script not found at $Runner" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# StartWhenAvailable is what makes a missed window recoverable rather than skipped.
# No ExecutionTimeLimit: this is a long-running service, and the default 3-day cap
# would kill it mid-week for no reason.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

"Registered '$TaskName' -- starts at logon, serves http://127.0.0.1:8765"
"Log: $(Join-Path $RepoRoot 'data\scheduler-service.log')"
"Remove with: powershell -ExecutionPolicy Bypass -File scripts\install_scheduler_task.ps1 -Uninstall"
