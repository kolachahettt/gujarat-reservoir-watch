<#
.SYNOPSIS
  Register (or remove) the Windows scheduled task that refreshes the site.

.DESCRIPTION
  The source refuses GitHub-hosted runners (PROJECT_BRIEF §17), so the daily
  refresh runs here instead. This registers a task that calls
  scripts\daily_update.cmd once a day.

  Deliberate choices:

  * Default 09:15 local. The report for a given day appears on the portal
    during the following morning IST - on 12 Sep 2026 it was absent at
    08:38 UTC and present by 09:30 UTC. The task targets YESTERDAY, so it has
    a full day of slack; 09:15 simply avoids asking before there is anything
    to ask for.
  * StartWhenAvailable. A laptop that was asleep at 09:15 runs the task when
    it next wakes instead of silently skipping the day. This is the single
    most important setting here: without it, a closed lid is a missing day.
  * No WakeToRun. Waking a machine to poll a government portal is not worth
    it; the catch-up above covers it.
  * Runs only on AC power is NOT set - the default would skip on battery.
  * Runs whether or not you are logged on is NOT used, because that needs a
    stored password. It runs as you, in your session, using your cached git
    credential. That is the trade: no secret at rest, but you must be logged
    in for it to fire.
  * One instance at a time, and a 2-hour execution limit so a hung fetch
    cannot sit forever.

.EXAMPLE
  # from the repository root, in a normal (non-admin) PowerShell:
  powershell -ExecutionPolicy Bypass -File scripts\register_daily_task.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register_daily_task.ps1 -At 07:30

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register_daily_task.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$TaskName = "GujaratReservoirWatch-DailyUpdate",
    [string]$At = "09:15",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$cmd  = Join-Path $repo "scripts\daily_update.cmd"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed scheduled task '$TaskName'"
    } else {
        Write-Host "no scheduled task named '$TaskName'"
    }
    return
}

if (-not (Test-Path $cmd)) { throw "missing $cmd" }

# Prove it runs before scheduling it. A task registered against a broken
# command is worse than no task: it reports success at the scheduler level
# while doing nothing useful.
Write-Host "checking the wrapper runs (dry run, no commit)..." -ForegroundColor Cyan
& $cmd --dry-run | Select-Object -Last 6
if ($LASTEXITCODE -ne 0) {
    throw "dry run exited $LASTEXITCODE - fix that before scheduling. See logs\daily_update.log"
}
Write-Host "dry run OK" -ForegroundColor Green

$action = New-ScheduledTaskAction -Execute $cmd -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description @"
Gujarat Reservoir Watch daily refresh. Fetches one WRD daily report, verifies
it against the report's own grand total, applies the capacity gate, rebuilds
the site JSON and pushes. Runs locally because the source is unreachable from
GitHub-hosted runners. Log: $repo\logs\daily_update.log
"@ -Force | Out-Null

Write-Host ""
Write-Host "registered '$TaskName', daily at $At" -ForegroundColor Green
Write-Host "  command : $cmd"
Write-Host "  log     : $repo\logs\daily_update.log"
Write-Host "  outcomes: $repo\logs\daily_update_runs.log"
Write-Host ""
Write-Host "run it now without waiting for the trigger:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "check the last result:"
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host ""
Write-Host "Note: it runs in your logged-on session and uses your cached git"
Write-Host "credential, so no password is stored anywhere - but it will not"
Write-Host "fire while you are logged out."
