# Starts the FO Intelligence Agent web service with its scheduler armed.
#
# Registered as a Windows Task Scheduler task that fires at logon (see
# scripts/install_scheduler_task.ps1), which is what makes scheduled runs happen
# without a hosting bill: the agent needs this machine anyway -- its SQLite database,
# its tool credentials and its LinkedIn scraper all live here -- so the timer belongs
# here too.
#
# Missed windows are NOT lost. app/scheduler.due_schedules fires anything whose
# next_run_at has already passed, so a 03:00 schedule missed because the machine was
# off runs shortly after the next logon.
#
# Arming the loop does NOT by itself spend anything: with no schedules defined, the
# loop wakes, finds nothing due, and sleeps. Runs only happen once you create a
# schedule in the Log tab -- deliberately, because each scheduled run makes real paid
# Serper/Snov/LLM calls.

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Port     = 8765
$LogDir   = Join-Path $RepoRoot 'data'
$LogFile  = Join-Path $LogDir 'scheduler-service.log'

# Run through the repo's venv, NOT the system interpreter. This machine's Python is a
# Microsoft Store install, and neither of its two forms survives a scheduled task:
# the real binary under C:\Program Files\WindowsApps\... is ACL-locked ("Permission
# denied"), and the WindowsApps alias exits immediately in a non-interactive session.
# Both were observed here -- the task wrote its "starting" line and died with exit
# code 1 before uvicorn produced a single byte. The venv's python.exe is an ordinary
# file with ordinary permissions, so it just runs.
#
# The venv was created with --system-site-packages, so it reuses the already-installed
# dependencies rather than duplicating ~1GB of torch/playwright.
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $Python)) {
    throw "No venv at $Python. Create it with: python -m venv .venv --system-site-packages"
}

Set-Location $RepoRoot
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$env:FOIA_SCHEDULER = '1'
$env:FOIA_SCHEDULER_POLL_SECONDS = '60'
$env:PYTHONPATH = $RepoRoot
$env:PYTHONUNBUFFERED = '1'

"[$(Get-Date -Format o)] starting on port $Port with $Python" | Out-File -FilePath $LogFile -Append -Encoding utf8

# uvicorn logs to STDERR, including its ordinary "Started server process" banner. With
# $ErrorActionPreference = 'Stop' still in force, PowerShell promotes any native-command
# stderr write to a terminating NativeCommandError -- so the service was killed by its
# own healthy startup message, exit code 1, before serving a single request. Dropping
# back to 'Continue' for the exec is the fix: a log line on stderr is not an error.
$ErrorActionPreference = 'Continue'

# 127.0.0.1, not 0.0.0.0: this listens for you, not for the network. Nothing here
# authenticates, and POST /api/run spends money.
& $Python -m uvicorn app.api:app --host 127.0.0.1 --port $Port --log-level info *>&1 |
    Out-File -FilePath $LogFile -Append -Encoding utf8
