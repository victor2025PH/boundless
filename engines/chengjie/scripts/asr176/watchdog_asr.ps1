# Self-heal watchdog for the GPU audio service (every 5 min).
# If /health does not answer within 8s, bounce the service through its scheduled task.
# Rationale: AITR_ASR_* is ONSTART-only — a mid-day crash (or the default 72h
# ExecutionTimeLimit closing the console) silently degrades clients to fallback.
# 2026-08-29 service moved 176 -> 198 (AITR_ASR_198). Prefer that task if present.
# Register (as admin, on the ASR host):
#   schtasks /Create /F /TN 'AITR_ASR_WATCHDOG' /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST `
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\aitr_asr\watchdog_asr.ps1"
$ErrorActionPreference = 'Continue'
$log = 'C:\aitr_asr\logs\watchdog.log'
$task = 'AITR_ASR_176'
try {
    if (Get-ScheduledTask -TaskName 'AITR_ASR_198' -ErrorAction SilentlyContinue) {
        $task = 'AITR_ASR_198'
    }
} catch { }

try {
    $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 8 -UseBasicParsing
    if ($resp.StatusCode -eq 200) { exit 0 }   # healthy: stay quiet, no log spam
    $reason = "status=$($resp.StatusCode)"
} catch {
    $reason = "unreachable: $($_.Exception.Message)"
}

$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "[$ts] health failed ($reason) -> restarting $task"
schtasks /End /TN $task 2>$null | Out-Null
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'asr_server' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 1
schtasks /Run /TN $task | Out-Null
$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "[$ts] restart issued"
