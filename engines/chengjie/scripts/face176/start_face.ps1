# start_face.ps1 -- scheduled-task entrypoint for the face-embed service (run ON 176)
# Idempotence gate = /health 200 (a zombie that listens but cannot accept is reaped),
# same discipline as start_ocr.ps1 / _svc_emotion_boot.bat (2026-07-14 half-dead lesson).
$ErrorActionPreference = "SilentlyContinue"
$root = "C:\aitr_face"
$log  = "$root\logs\face.out.log"

try {
  $h = Invoke-RestMethod -Uri "http://127.0.0.1:8767/health" -TimeoutSec 3
  if ($h.ok) { exit 0 }   # already healthy
} catch {}

# reap any stale listener on 8767
$conn = Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue
if ($conn) { $conn | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }

# rotate log > 20MB
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 20MB)) {
  Move-Item -Force $log "$root\logs\face.out.1.log"
}

$env:AITR_WARMUP = "1"
$env:AITR_FACE_PORT = "8767"
$env:AITR_FACE_MODEL = "antelopev2"
$env:AITR_FACE_ROOT = "D:\ComfyUI\models\insightface"
$env:AITR_FACE_DET_SIZE = "640"
$env:PYTHONIOENCODING = "utf-8"
Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
  -ArgumentList "$root\face_server.py" -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput $log -RedirectStandardError "$root\logs\face.err.log"
