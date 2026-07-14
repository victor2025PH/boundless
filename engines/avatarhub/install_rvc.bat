@echo off
setlocal EnableDelayedExpansion

echo ============================================================
echo  RVC v2 (WebUI) Installation Script - Phase 2
echo  Target: conda env "rvc", CUDA 11.8, RTX 4070
echo ============================================================
echo.

:: -- 0. Pre-flight checks ----------------------------------------

:: Check conda
where conda >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] conda not found in PATH.
    echo         Please install Miniconda or Anaconda first:
    echo         https://docs.conda.io/en/latest/miniconda.html
    pause
    exit /b 1
)

:: Check git
where git >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] git not found in PATH.
    echo         Please install Git for Windows: https://git-scm.com/download/win
    pause
    exit /b 1
)

:: Check ffmpeg (required by RVC for audio I/O)
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] ffmpeg not found in PATH.
    echo         RVC requires FFmpeg for audio processing.
    echo         Download: https://www.gyan.dev/ffmpeg/builds/
    echo         Unzip and add the \bin folder to your system PATH, then re-run.
    pause
    exit /b 1
)

:: -- 1. Create / reset conda environment ------------------------

echo [1/6] Creating conda environment "rvc" with Python 3.10...
call conda create -n rvc python=3.10 -y
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create conda environment "rvc".
    pause
    exit /b 1
)

:: -- 2. Activate environment -------------------------------------

echo [2/6] Activating environment...
call conda activate rvc
if %errorlevel% neq 0 (
    echo [ERROR] Failed to activate conda environment "rvc".
    echo         Make sure conda is initialised for cmd.exe: conda init cmd.exe
    pause
    exit /b 1
)

:: -- 3. Install PyTorch (CUDA 12.8 wheel by default) -------------
::    Deployed freeze is +cu128 (required for RTX 50-series / sm_120).
::    Older GPUs: before running, set "TORCH_CUDA_INDEX=https://download.pytorch.org/whl/cu118"

echo [3/6] Installing PyTorch 2.x (CUDA 12.8 by default)...
if not defined TORCH_CUDA_INDEX set "TORCH_CUDA_INDEX=https://download.pytorch.org/whl/cu128"
echo     PyTorch index = %TORCH_CUDA_INDEX%
pip install torch torchvision torchaudio --index-url %TORCH_CUDA_INDEX%
if %errorlevel% neq 0 (
    echo [ERROR] PyTorch installation failed.
    pause
    exit /b 1
)

:: -- 4. Clone RVC WebUI ------------------------------------------

set "RVC_DIR=%~dp0Retrieval-based-Voice-Conversion-WebUI"

:: NOTE: tracks upstream @main (not pinned). For a fully reproducible build, pin a validated
::       commit after clone:  git -C "%RVC_DIR%" checkout <commit>
if exist "%RVC_DIR%\.git" (
    echo [4/6] RVC repo already exists - pulling latest changes...
    git -C "%RVC_DIR%" pull
) else (
    echo [4/6] Cloning RVC WebUI repository...
    git clone https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI.git "%RVC_DIR%"
)

if %errorlevel% neq 0 (
    echo [ERROR] Git operation failed for RVC.
    pause
    exit /b 1
)

cd /d "%RVC_DIR%"

:: -- 5. Install base requirements --------------------------------

echo [5/6] Installing requirements.txt...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [WARN] requirements.txt install reported errors. Attempting to continue...
)

:: -- 6. Install additional audio/ML packages ---------------------

echo [6/6] Installing faiss-cpu, crepe, and pyaudio...

:: faiss-cpu  - vector search for speaker retrieval index
pip install faiss-cpu
if %errorlevel% neq 0 (
    echo [WARN] faiss-cpu install failed. Trying faiss-gpu as fallback...
    pip install faiss-gpu
)

:: crepe - pitch estimation model (required for pitch-shifting quality)
pip install crepe
if %errorlevel% neq 0 (
    echo [WARN] crepe install failed. Pitch tracking may fall back to harvest/dio.
)

:: pyaudio - real-time mic/speaker I/O for live inference
pip install pyaudio
if %errorlevel% neq 0 (
    echo [WARN] pyaudio install failed.
    echo        If you see PortAudio errors, install it via conda:
    echo          conda install -n rvc -c conda-forge pyaudio -y
)

echo.
echo ============================================================
echo  RVC v2 installation COMPLETE.
echo  Next steps:
echo    1. Place your trained .pth model in:
echo       %RVC_DIR%\assets\weights\
echo    2. Place the corresponding .index file in:
echo       %RVC_DIR%\logs\
echo    3. Run start_rvc.bat to launch the WebUI.
echo ============================================================
pause
endlocal
