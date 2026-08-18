<#
    scripts/pc_fallback.ps1
    ───────────────────────
    Safety net for the cloud job. Run by Windows Task Scheduler at 23:45 IST.

    It does NOT duplicate work. run_engine_cli.py checks market_calendar first,
    so if GitHub Actions already loaded the day this exits in a couple of
    seconds having done nothing. It only actually runs when the cloud job was
    blocked, failed, or never fired.

    --catchup 5 means it also fills in any of the previous four weekdays that
    are neither loaded nor a known holiday — so a few days of your PC being off
    repairs itself the next time it is on, with no manual backfilling.

    Install (PowerShell, run once, as your normal user):

        cd C:\Users\user-pc\Downloads\trendplus-admin-approval-auth
        powershell -ExecutionPolicy Bypass -File scripts\pc_fallback.ps1 -Install

    Remove:

        powershell -ExecutionPolicy Bypass -File scripts\pc_fallback.ps1 -Uninstall

    Test right now:

        powershell -ExecutionPolicy Bypass -File scripts\pc_fallback.ps1
#>

param(
    [switch]$Install,
    [switch]$Uninstall,
    [int]$Catchup = 5,
    [string]$TaskName = "TrendPulse Daily Fallback",
    [string]$At = "23:45"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python   = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Script   = Join-Path $RepoRoot "scripts\run_engine_cli.py"
$LogDir   = Join-Path $RepoRoot "logs"

# ── Install / uninstall the scheduled task ───────────────────────────
if ($Install) {
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Catchup $Catchup" `
        -WorkingDirectory $RepoRoot

    $trigger = New-ScheduledTaskTrigger -Daily -At $At

    # StartWhenAvailable: if the PC was asleep at 23:45, run as soon as it wakes.
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Runs the TrendPulse engine only if the cloud job did not." -Force | Out-Null

    Write-Host "Installed scheduled task '$TaskName' — daily at $At, catchup $Catchup weekdays."
    Write-Host "Verify with:  Get-ScheduledTask -TaskName '$TaskName'"
    exit 0
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

# ── Normal run ───────────────────────────────────────────────────────
if (-not (Test-Path $Python)) {
    Write-Error "Python not found at $Python — create the venv first, or edit `$Python in this script."
    exit 1
}
if (-not (Test-Path $Script)) {
    Write-Error "run_engine_cli.py not found at $Script"
    exit 1
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$log = Join-Path $LogDir ("engine_" + (Get-Date -Format "yyyyMMdd") + ".log")

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  PC fallback starting (catchup $Catchup) ===" |
    Tee-Object -FilePath $log -Append

Push-Location $RepoRoot
try {
    & $Python $Script --catchup $Catchup 2>&1 | Tee-Object -FilePath $log -Append
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}

"=== finished with exit code $code ===" | Tee-Object -FilePath $log -Append

# Exit code 2 = NSE refused the download from here too, which would be unusual
# on a home connection and is worth looking at.
if ($code -ne 0) {
    Write-Warning "Engine exited $code — see $log"
}
exit $code
