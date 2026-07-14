# Self-heal watchdog for ComfyUI on 176 (run ON 176 via schtasks, every 5 min).
# If /system_stats does not answer within 10s, bounce ComfyUI through its
# scheduled task (ComfyBoot). Rationale: ComfyBoot is an interactive console
# task — an accidental window close (real incident 2026-07-14: forrtl error 200
# "window-CLOSE event", schtasks last result 0xC000013A) kills it silently and
# the firewall then DROPs port 8188, so image generation on all accounts
# degrades to text excuses until someone notices.
# Register (on 176, as the interactive user; SYSTEM also fine if admin):
#   schtasks /Create /F /TN 'ComfyWatchdog' /SC MINUTE /MO 5 `
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File D:\ComfyUI\watchdog_comfy.ps1"
$ErrorActionPreference = 'Continue'
$log = 'D:\ComfyUI\watchdog.log'
$stamp = 'D:\ComfyUI\watchdog.last_restart'

try {
    $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8188/system_stats' -TimeoutSec 10 -UseBasicParsing
    if ($resp.StatusCode -eq 200) { exit 0 }   # healthy: stay quiet, no log spam
    $reason = "status=$($resp.StatusCode)"
} catch {
    $reason = "unreachable: $($_.Exception.Message)"
}

# Anti-flap: ComfyUI boot takes ~40-60s (and first jobs may stream weights);
# don't hammer restarts more often than every 10 minutes.
if (Test-Path $stamp) {
    $age = (Get-Date) - (Get-Item $stamp).LastWriteTime
    if ($age.TotalMinutes -lt 10) { exit 0 }
}

$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "[$ts] system_stats failed ($reason) -> restarting"
schtasks /End /TN 'ComfyBoot' 2>$null | Out-Null
# Belt & suspenders: kill any leftover comfyui-env python (wedged process can
# survive task end; a fresh boot needs the port and the VRAM back).
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.ExecutablePath -like '*envs\comfyui*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
schtasks /Run /TN 'ComfyBoot' | Out-Null
New-Item -ItemType File -Path $stamp -Force | Out-Null
$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "[$ts] restart issued"
