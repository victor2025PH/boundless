# FireRedTTS3 环境安装 + 权重下载（104 声音机；幂等可重入）。
# 只做增置：不停不动现役 IndexTTS-2 :7865；服务启动/切换是下一步的显式动作。
# 用法（本机管理员 PowerShell）：powershell -ExecutionPolicy Bypass -File C:\firered\deploy_104.ps1
$ErrorActionPreference = "Continue"
# sshd 会话无控制台缓冲：PS5.1 的 Write-Progress/Invoke-WebRequest 进度条会抛
# 0x5 HostException（2026-08-31 首跑实锤）——静默进度 + 下载一律走 curl.exe。
$ProgressPreference = "SilentlyContinue"
$Root = "C:\firered"
$Log = Join-Path $Root "deploy.log"
New-Item -ItemType Directory -Force $Root | Out-Null

function Say($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Write-Host $line
    Add-Content -Path $Log -Value $line -Encoding UTF8
}

Say "=== FireRedTTS3 deploy kickoff ==="

# ── 1) uv（单文件包管理器；自带 CPython 下载，不碰系统 Python）────────────────
$Uv = Join-Path $Root "uv\uv.exe"
if (-not (Test-Path $Uv)) {
    Say "downloading uv (curl.exe)..."
    $zip = Join-Path $Root "uv.zip"
    & curl.exe -sSL -o $zip "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" 2>&1 | ForEach-Object { Say $_ }
    Expand-Archive -Path $zip -DestinationPath (Join-Path $Root "uv") -Force
    Remove-Item $zip -Force
}
Say ("uv: " + (& $Uv --version))

# ── 2) venv（CPython 3.11：FireRed 上游钉 3.10/3.11；uv 自动拉运行时）─────────
$Venv = Join-Path $Root "venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Say "creating venv (py3.11)..."
    & $Uv venv $Venv --python 3.11 2>&1 | ForEach-Object { Say $_ }
}
$Py = Join-Path $Venv "Scripts\python.exe"
Say ("venv python: " + (& $Py --version))

# ── 3) 代码 + 依赖（torch 2.8 cu128 = 上游钉死；flash-attn 缺席时上游回落 sdpa 由
#      wrapper 启动参数处理——Windows 上 flash-attn 轮子难求，先不装）──────────
$Repo = Join-Path $Root "FireRedTTS3"
if (-not (Test-Path (Join-Path $Repo ".git"))) {
    Say "cloning FireRedTTS3..."
    git clone --depth 1 https://github.com/FireRedTeam/FireRedTTS3 $Repo 2>&1 | ForEach-Object { Say $_ }
}
Say "installing torch cu128 (large download, be patient)..."
& $Uv pip install --python $Py torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128 2>&1 |
    Select-Object -Last 5 | ForEach-Object { Say $_ }
Say "installing FireRedTTS3 package..."
Push-Location $Repo
& $Uv pip install --python $Py -e . 2>&1 | Select-Object -Last 5 | ForEach-Object { Say $_ }
Pop-Location
& $Uv pip install --python $Py "huggingface_hub[cli]" fastapi uvicorn 2>&1 |
    Select-Object -Last 3 | ForEach-Object { Say $_ }

# ── 4) 权重（20.8GB；hf CLI 断点续传；国内走 hf-mirror）──────────────────────
$env:HF_ENDPOINT = "https://hf-mirror.com"
$Models = Join-Path $Root "pretrained_models"
Say "downloading FireRedTeam/FireRedTTS3 weights (20.8GB, resumable)..."
& (Join-Path $Venv "Scripts\hf.exe") download FireRedTeam/FireRedTTS3 --local-dir $Models 2>&1 |
    Select-Object -Last 8 | ForEach-Object { Say $_ }
if (-not (Test-Path $Models)) {
    Say "hf.exe path fallback -> python -m huggingface_hub"
    & $Py -m huggingface_hub.commands.huggingface_cli download FireRedTeam/FireRedTTS3 --local-dir $Models 2>&1 |
        Select-Object -Last 8 | ForEach-Object { Say $_ }
}

$size = (Get-ChildItem $Models -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1GB
Say ("weights on disk: {0:N1} GB (expect ~20.8)" -f $size)
Say "=== kickoff done. NEXT: wrapper 定稿 + 切换验收窗口（见 README）==="
