# Register a Windows Scheduled Task that runs triage_watch every N hours.
# ASCII-only (avoid PS5.1 GBK decode pitfalls). Idempotent: re-run to update.
#
#   Register (default 6h):  powershell -ExecutionPolicy Bypass -File scripts\register_triage_task.ps1
#   Custom instance/log:    ... -Instance zhiliao -LogPath 'D:\chengjie-instances\zhiliao\data\logs\app.log'
#   Run once now:           ... -RunNow
#   Remove:                 ... -Unregister
#
# The task runs: python -m scripts.triage_watch --window-hours <H> --alert
# It only alerts (host_alert popup + app.log + EventBus) on NEW/SURGING error classes.

param(
    [string]$Instance = "zhiliao",
    [string]$LogPath = "",
    [double]$Hours = 6,
    [string]$EngineRoot = "D:\boundless\engines\chengjie",
    [switch]$RunNow,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "ChengjieTriageWatch_$Instance"

if (-not $LogPath) {
    $LogPath = "D:\chengjie-instances\$Instance\data\logs\app.log"
}

if ($Unregister) {
    schtasks /Delete /TN $TaskName /F 2>$null | Out-Null
    Write-Output "[triage-task] unregistered $TaskName"
    exit 0
}

# Find python
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = "python" }

if ($RunNow) {
    # Executed by the scheduled task (or manually). Set env + run the watcher once.
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:AITR_TRIAGE_LOG = $LogPath
    Set-Location $EngineRoot
    Write-Output "[triage-task] running once against $LogPath"
    & $py -m scripts.triage_watch --window-hours $Hours --alert
    Write-Output "[triage-task] run exit=$LASTEXITCODE"
    exit $LASTEXITCODE
}

# The task simply re-invokes THIS script with -RunNow (keeps /TR under the 261-char limit).
$action = "powershell.exe -ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden " +
          "-File `"$PSCommandPath`" -Instance $Instance -Hours $Hours -RunNow"

# Register: every N hours, starting at next quarter hour, run whether or not user is logged on.
$minutes = [int]($Hours * 60)
$start = (Get-Date).AddMinutes(5).ToString("HH:mm")

schtasks /Create /TN $TaskName /TR $action /SC MINUTE /MO $minutes /ST $start /F /RL LIMITED | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "[triage-task] schtasks create failed (exit=$LASTEXITCODE)"
    exit 1
}
Write-Output "[triage-task] registered $TaskName : every ${Hours}h (=$minutes min), start $start"
Write-Output "[triage-task] target log: $LogPath"
Write-Output "[triage-task] verify:  schtasks /Query /TN $TaskName /V /FO LIST"
Write-Output "[triage-task] run now:  powershell -ExecutionPolicy Bypass -File scripts\register_triage_task.ps1 -Instance $Instance -RunNow"
