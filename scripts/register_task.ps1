<#
.SYNOPSIS
    Register (or re-register) the daily automation as a Windows Scheduled Task.

.DESCRIPTION
    Creates a task that runs `daily-automation run` once a day using the
    project's virtual environment.

    Two settings matter for a personal machine that is not always on:
      -WakeToRun            wakes the machine from sleep for the run
      StartWhenAvailable    runs as soon as possible after a missed start,
                            so a laptop that was shut at 09:00 still catches up

    The task runs only when the user is logged on, because the OAuth token is
    stored in the user profile and a run needs the user's own credentials.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -At 07:30
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Unregister
#>

param(
    [string]$TaskName = "DailyAIAutomation",
    [string]$At = "09:00",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$logDir = Join-Path $projectRoot "data\logs"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName' exists."
    }
    return
}

if (-not (Test-Path $python)) {
    throw "Virtual environment not found at $python. Create it first: python -m venv .venv"
}

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

# The scheduler has no console, so stdout/stderr are redirected to a file.
# The structured JSON log is written separately by the application itself.
$command = "& '$python' -m daily_ai_automation.main run *>> '$logDir\scheduled-run.log'"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded" `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Daily AI Supervisor: Gmail triage and occasion greetings." | Out-Null

Write-Host "Registered '$TaskName' to run daily at $At."
Write-Host "Console output: $logDir\scheduled-run.log"
Write-Host ""
Write-Host "Run it once now to confirm:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check the outcome:           Get-ScheduledTaskInfo -TaskName $TaskName"
