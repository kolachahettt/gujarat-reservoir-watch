@echo off
REM ---------------------------------------------------------------------------
REM Task Scheduler wrapper for the daily update.
REM
REM Exists so the scheduled task has one stable thing to call, and so the
REM working directory and console encoding are right regardless of what
REM Task Scheduler decides to hand us. Task Scheduler starts a job in
REM %SystemRoot%\system32 by default, which would break every relative path
REM in the pipeline.
REM
REM Run it by hand exactly as the scheduler will:
REM     scripts\daily_update.cmd
REM Pass anything through to the Python:
REM     scripts\daily_update.cmd --dry-run
REM     scripts\daily_update.cmd --date 2026-09-11 --no-push
REM
REM Exit codes are passed through unchanged: 0 fine, 1 loud failure,
REM 2 capacity gate tripped. Task Scheduler shows the last result, so a
REM non-zero code is visible in the Task Scheduler UI without opening the log.
REM ---------------------------------------------------------------------------

REM UTF-8, so the log reads correctly in a console.
chcp 65001 >nul 2>&1

REM Anchor to the repository root, which is this script's parent.
pushd "%~dp0.." || exit /b 1

if not exist "logs" mkdir "logs"

REM Prefer the py launcher; fall back to whatever python is on PATH.
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 scripts\daily_update.py %*
) else (
    python scripts\daily_update.py %*
)
set RC=%ERRORLEVEL%

REM A one-line outcome, so tailing this file alone tells you the history
REM without reading the full log.
for /f "tokens=* usebackq" %%t in (`powershell -NoProfile -Command "Get-Date -Format o"`) do set NOW=%%t
>>"logs\daily_update_runs.log" echo %NOW%  exit=%RC%

popd
exit /b %RC%
