@echo off
setlocal EnableDelayedExpansion

echo ============================================================
echo  RVC v2 WebUI Launcher - Real-Time Voice Conversion Mode
echo  GPU: device 0 (RTX 4070)   Expected VRAM usage: ~1-2 GB
echo ============================================================
echo.

set "RVC_DIR=%~dp0Retrieval-based-Voice-Conversion-WebUI"

if not exist "%RVC_DIR%\infer-web.py" (
    echo [ERROR] infer-web.py not found in %RVC_DIR%
    echo         Please run install_rvc.bat first.
    pause
    exit /b 1
)

:: Activate the dedicated conda environment
call conda activate rvc
if %errorlevel% neq 0 (
    echo [ERROR] Could not activate conda env "rvc".
    pause
    exit /b 1
)

cd /d "%RVC_DIR%"

:: Force PyTorch to use GPU 0 exclusively
set CUDA_VISIBLE_DEVICES=0

:: Optional: limit PyTorch CUDA memory fraction so it never exceeds ~2 GB
:: RVC uses ~1-2 GB; this gives a hard ceiling and leaves the rest for FaceFusion.
:: Remove or increase this env var if you are running RVC standalone.
set PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:512

echo [INFO] Starting RVC WebUI...
echo [INFO] GPU              : CUDA device 0
echo [INFO] CUDA alloc limit : max_split_size_mb=512
echo [INFO] Open the printed URL in your browser to access the UI.
echo.

python infer-web.py --pycmd python --port 7865 --noautoopen

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] RVC exited with code %errorlevel%.
    echo         Common causes:
    echo           - Missing .pth model in assets\weights\
    echo           - PyAudio / PortAudio not installed (run: conda install -c conda-forge pyaudio)
    echo           - CUDA OOM: close FaceFusion or reduce its --memory-limit value
)

pause
endlocal
