# 只在 117 的 zl_accept venv 里下载并加载 devanagari_PP-OCRv5_mobile_rec。
# 不启动、不停止、不修改共享的 OCR 进程。模型缓存只放在这个 venv 里面。
param(
    [string]$Venv = "C:\zl_accept\venv"
)

$ErrorActionPreference = "Stop"
if ($Venv -notmatch 'zl_accept') {
    Write-Error "拒绝执行：venv 路径必须包含 zl_accept，避免装进共享 OCR 环境。"
    exit 2
}
$python = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "找不到 $python"
    exit 2
}
$cache = Join-Path $Venv "paddleocr-cache"
New-Item -ItemType Directory -Force -Path $cache | Out-Null
$env:PADDLE_PDX_CACHE_HOME = $cache
& $python -m pip install "paddleocr" "pillow" "numpy"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -c @"
import os
os.environ['PADDLE_PDX_CACHE_HOME'] = r'$cache'
from paddleocr import PaddleOCR
PaddleOCR(
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=True,
    enable_mkldnn=False,
    text_detection_model_name='PP-OCRv5_mobile_det',
    text_recognition_model_name='devanagari_PP-OCRv5_mobile_rec',
)
print('devanagari model ready')
print('PADDLE_PDX_CACHE_HOME=' + os.environ['PADDLE_PDX_CACHE_HOME'])
"@
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "set WA_DEVANAGARI_OCR=1"
Write-Output "set PADDLE_PDX_CACHE_HOME=$cache"
