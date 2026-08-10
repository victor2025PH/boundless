@echo off
rem Provision Qwen3-TTS VoiceDesign service on a GPU host (idempotent, ASCII only).
rem Recipe reuses the proven faceX qwen3tts setup (.117); target any 16GB+ free GPU
rem (recommended: .173 5090 32G). DO NOT install on the already-full 117/3060.
rem Usage:  provision_qwen3_vd.bat [BASE_DIR]     (default D:\faceX)
chcp 65001 >nul
set BASE=%~1
if "%BASE%"=="" set BASE=D:\faceX
set CONDA=%BASE%\Miniconda3\Scripts\conda.exe
set ENVDIR=%BASE%\Miniconda3\envs\qwen3tts
set PY=%ENVDIR%\python.exe
set PIP=%ENVDIR%\Scripts\pip.exe
set TARGET=%BASE%\models\Qwen3-TTS-12Hz-1.7B-VoiceDesign

if exist %PY% goto :pipinstall
echo [1/4] creating conda env qwen3tts (py310)...
%CONDA% create -n qwen3tts python=3.10 -y
if errorlevel 1 exit /b 1

:pipinstall
echo [2/4] installing torch cu128 + qwen-tts...
%PIP% install torch --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 exit /b 2
%PIP% install -U qwen-tts transformers soundfile numpy fastapi uvicorn requests pydantic huggingface_hub modelscope
if errorlevel 1 exit /b 3

echo [3/4] downloading VoiceDesign 1.7B weights via modelscope (CN-friendly)...
if exist %TARGET%\model.safetensors goto :verify
%PY% -c "from modelscope import snapshot_download; p=snapshot_download('Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign', local_dir=r'%TARGET%'); print('downloaded to', p)"
if errorlevel 1 exit /b 4

:verify
echo [4/4] verify imports...
%PY% -c "import torch; print('TORCH', torch.__version__, 'cuda_ok', torch.cuda.is_available())"
%PY% -c "import qwen_tts; print('QWEN_TTS OK')"
echo.
echo PROVISION_DONE. Start the service with:
echo   set QWEN3_VD_MODEL_DIR=%TARGET%
echo   %PY% %~dp0qwen3_vd_server.py
echo Selftest (no server):
echo   %PY% %~dp0qwen3_vd_server.py --selftest "warm young female voice, slightly slow, smiling"
