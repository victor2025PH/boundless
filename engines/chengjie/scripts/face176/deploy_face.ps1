# deploy_face.ps1 -- idempotent deploy of the face-embed microservice on 176 (run ON 176)
# Mirrors C:\aitr_ocr / C:\aitr_asr pattern: uv venv (py3.12) + deps + firewall + schtasks + start.
# CPU inference by design (5090 VRAM is chronically ~26/32GB; QPS here is tiny).
# Models: reuses PuLID's InsightFace antelopev2 already on this box (D:\ComfyUI\models\insightface).
$ErrorActionPreference = "Stop"
$env:Path = "C:\Users\user\.local\bin;$env:Path"
$root = "C:\aitr_face"
New-Item -ItemType Directory -Force -Path $root, "$root\logs" | Out-Null

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
  Set-Location $root
  uv venv --python 3.12 .venv
  if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
}
$py = "$root\.venv\Scripts\python.exe"

# insightface 1.x ships py3.12 wheels; onnxruntime CPU; headless cv2 (no GUI deps)
uv pip install --python $py --upgrade `
  insightface onnxruntime opencv-python-headless numpy fastapi uvicorn pydantic
if ($LASTEXITCODE -ne 0) { throw "uv pip install failed" }

if (-not (Test-Path "D:\ComfyUI\models\insightface\models\antelopev2\glintr100.onnx")) {
  throw "antelopev2 onnx not found under D:\ComfyUI\models\insightface\models\antelopev2 (set AITR_FACE_ROOT/AITR_FACE_MODEL in start_face.ps1)"
}

New-NetFirewallRule -DisplayName "AITR FACE 8767" -Direction Inbound -Protocol TCP `
  -LocalPort 8767 -Action Allow -ErrorAction SilentlyContinue | Out-Null

schtasks /Create /TN AITR_FACE_176 /SC ONSTART /RU SYSTEM /RL HIGHEST /F `
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File $root\start_face.ps1" | Out-Null
schtasks /Create /TN AITR_FACE_WATCHDOG /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST /F `
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File $root\watchdog_face.ps1" | Out-Null

schtasks /Run /TN AITR_FACE_176 | Out-Null
Write-Host "deploy done; waiting /health ..."
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
  try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:8767/health" -TimeoutSec 3
    Write-Host ("health: ok=" + $h.ok + " loaded=" + $h.loaded + " model=" + $h.model + " err=" + $h.error)
    if ($h.ok) { break }
  } catch { }
  Start-Sleep -Seconds 3
}
