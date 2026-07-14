@echo off
setlocal EnableDelayedExpansion

echo ============================================================
echo  FaceFusion Installation Script - Phase 1
echo  Target: conda env "facefusion", CUDA 11.8, RTX 4070
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

:: Check ffmpeg (warn only - FaceFusion can install it, but having it globally helps)
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] ffmpeg not found in PATH.
    echo        It is strongly recommended to install FFmpeg globally.
    echo        Download: https://www.gyan.dev/ffmpeg/builds/
    echo        Add the \bin folder to your system PATH, then re-run this script.
    echo        Continuing anyway - FaceFusion may install its own copy via pip...
    echo.
)

:: -- 1. Create / reset conda environment ------------------------

echo [1/5] Creating conda environment "facefusion" with Python 3.11...
call conda create -n facefusion python=3.11 -y
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create conda environment.
    pause
    exit /b 1
)

:: -- 2. Activate environment -------------------------------------

echo [2/5] Activating environment...
call conda activate facefusion
if %errorlevel% neq 0 (
    echo [ERROR] Failed to activate conda environment "facefusion".
    echo         Make sure conda is properly initialised for cmd.exe:
    echo           conda init cmd.exe
    pause
    exit /b 1
)

:: -- 3. Install PyTorch (CUDA 12.8 wheel by default) -------------
::    Deployed freeze is +cu128 (required for RTX 50-series / sm_120).
::    Older GPUs: before running, set "TORCH_CUDA_INDEX=https://download.pytorch.org/whl/cu118"

echo [3/5] Installing PyTorch 2.x (CUDA 12.8 by default)...
if not defined TORCH_CUDA_INDEX set "TORCH_CUDA_INDEX=https://download.pytorch.org/whl/cu128"
echo     PyTorch index = %TORCH_CUDA_INDEX%
pip install torch torchvision torchaudio --index-url %TORCH_CUDA_INDEX%
if %errorlevel% neq 0 (
    echo [ERROR] PyTorch installation failed.
    pause
    exit /b 1
)

:: -- 4. Clone FaceFusion -----------------------------------------

set "FF_DIR=%~dp0facefusion"

:: NOTE: tracks upstream @main (not pinned). For a fully reproducible build, pin a validated
::       commit after clone:  git -C "%FF_DIR%" checkout <commit>
if exist "%FF_DIR%\.git" (
    echo [4/5] FaceFusion repo already exists - pulling latest changes...
    git -C "%FF_DIR%" pull
) else (
    echo [4/5] Cloning FaceFusion repository...
    git clone https://github.com/facefusion/facefusion.git "%FF_DIR%"
)

if %errorlevel% neq 0 (
    echo [ERROR] Git operation failed for FaceFusion.
    pause
    exit /b 1
)

:: -- 5. Run FaceFusion installer ---------------------------------

echo [5/5] Running FaceFusion install.py (ONNX Runtime CUDA 11.8)...
cd /d "%FF_DIR%"
python install.py --onnxruntime cuda --skip-conda
if %errorlevel% neq 0 (
    echo [ERROR] FaceFusion install.py failed.
    echo         Check the output above for missing Visual C++ Build Tools.
    echo         Download VS Build Tools: https://visualstudio.microsoft.com/visual-cpp-build-tools/
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  FaceFusion installation COMPLETE.
echo  Run start_facefusion.bat to launch.
echo ============================================================
pause
endlocal
