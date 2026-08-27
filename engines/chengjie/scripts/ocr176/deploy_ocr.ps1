# deploy_ocr.ps1 -- idempotent deploy of PP-OCRv5 microservice on 176 (run ON 176)
# Mirrors C:\aitr_asr deploy pattern: uv venv (py3.12) + deps + firewall + schtasks + start.
# CPU inference by design (5090 VRAM is chronically ~26/32GB; QPS here is tiny).
$ErrorActionPreference = "Stop"
$root = "C:\aitr_ocr"
New-Item -ItemType Directory -Force -Path $root, "$root\logs" | Out-Null

if (-not (Test-Path "$root\.venv")) {
  Set-Location $root
  uv venv --python 3.12 .venv
}
$py = "$root\.venv\Scripts\python.exe"

# paddlepaddle CPU wheel + paddleocr (models auto-download from bcebos CDN on first predict)
uv pip install --python $py --upgrade `
  paddlepaddle paddleocr fastapi uvicorn pillow numpy python-multipart

New-NetFirewallRule -DisplayName "AITR OCR 8766" -Direction Inbound -Protocol TCP `
  -LocalPort 8766 -Action Allow -ErrorAction SilentlyContinue | Out-Null

# scheduled task: ONSTART, SYSTEM (same pattern as AITR_ASR_176)
schtasks /Create /TN AITR_OCR_176 /SC ONSTART /RU SYSTEM /RL HIGHEST /F `
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File $root\start_ocr.ps1" | Out-Null
schtasks /Create /TN AITR_OCR_WATCHDOG /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST /F `
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File $root\watchdog_ocr.ps1" | Out-Null

schtasks /Run /TN AITR_OCR_176 | Out-Null
Write-Host "deploy done; waiting /health ..."
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
  try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:8766/health" -TimeoutSec 3
    Write-Host ("health: ok=" + $h.ok + " loaded=" + $h.loaded + " engine=" + $h.engine)
    break
  } catch { Start-Sleep -Seconds 3 }
}
