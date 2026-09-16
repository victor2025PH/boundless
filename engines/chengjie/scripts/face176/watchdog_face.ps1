# watchdog_face.ps1 -- every 5min via schtask AITR_FACE_WATCHDOG (run ON 176)
# /health 8s no-answer -> restart via start_face.ps1 (which reaps zombies itself).
# Healthy = zero log zero action (same contract as watchdog_ocr.ps1). ASCII only.
$ErrorActionPreference = "SilentlyContinue"
$root = "C:\aitr_face"
$log = "$root\logs\watchdog.log"

try {
  $h = Invoke-RestMethod -Uri "http://127.0.0.1:8767/health" -TimeoutSec 8
  if ($h.ok) { exit 0 }
} catch {}

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Value "$ts health failed -> restart"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
  Move-Item -Force $log "$root\logs\watchdog.1.log"
}
powershell -NoProfile -ExecutionPolicy Bypass -File "$root\start_face.ps1"
