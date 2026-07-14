@echo off
setlocal EnableDelayedExpansion

echo ============================================================
echo  FaceFusion Launcher - Real-Time Streaming Mode
echo  VRAM Budget: strict strategy (RTX 4070 12GB)
echo ============================================================
echo.

:: Resolve FaceFusion directory relative to this script
set "FF_DIR=%~dp0facefusion"

if not exist "%FF_DIR%\facefusion.py" (
    echo [ERROR] facefusion.py not found in %FF_DIR%
    echo         Please run install_facefusion.bat first.
    pause
    exit /b 1
)

:: Ensure FFmpeg is available
set "PATH=C:\ffmpeg\bin;%PATH%"

:: Activate the dedicated conda environment
call conda activate facefusion
if %errorlevel% neq 0 (
    echo [ERROR] Could not activate conda env "facefusion".
    pause
    exit /b 1
)

cd /d "%FF_DIR%"

:: Bind Gradio to all interfaces so LAN clients can connect
set "GRADIO_SERVER_NAME=0.0.0.0"
set "GRADIO_SERVER_PORT=7860"

echo [INFO] Starting FaceFusion in webcam / live mode...
echo [INFO] Execution provider    : CUDA  (GPU 0)
echo [INFO] Video memory strategy : strict  (minimises VRAM, leaves room for RVC)
echo [INFO] UI layout             : webcam
echo [INFO] Server host           : 0.0.0.0  (LAN accessible)
echo [INFO] LAN address           : http://192.168.0.166:7860
echo.

:: -- Launch flags ------------------------------------------------
::   run                              -> launch the Gradio UI
::   --execution-providers cuda       -> use GPU inference
::   --execution-device-ids 0         -> pin to RTX 4070 (device 0)
::   --video-memory-strategy strict   -> aggressively free VRAM between frames
::   --ui-layouts webcam              -> open the live webcam swap layout
::   --server-host 0.0.0.0            -> bind to all interfaces (LAN access)
::   --open-browser                   -> auto-open the Gradio page in browser
python facefusion.py run ^
    --execution-providers cuda ^
    --execution-device-ids 0 ^
    --video-memory-strategy strict ^
    --ui-layouts webcam ^
    --open-browser

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] FaceFusion exited with code %errorlevel%.
    echo         If you see CUDA OOM errors, set --video-memory-strategy to "strict".
    echo         If models are missing, first run: python facefusion.py force-download
)

pause
endlocal
