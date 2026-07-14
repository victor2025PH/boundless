@echo off
chcp 65001 > nul
title Qwen3-TTS (clone TTS, port 7858)
cd /d "%~dp0"
rem Load env vars (CONDA_ROOT / per-env python / service token) + secrets
call "%~dp0env_config.bat"
rem Suppress OpenMP/MKL idle spin (GPU inference; no idle core burn)
set OMP_WAIT_POLICY=PASSIVE
set KMP_BLOCKTIME=0
echo [INFO] Qwen3-TTS starting on http://0.0.0.0:7858
echo [INFO] Model: %QWEN3_TTS_MODEL%  (default Qwen/Qwen3-TTS-12Hz-1.7B-Base; first run downloads weights)
if not exist "%QWEN3TTS_PY%" (
  echo [WARN] qwen3tts conda env python not found: %QWEN3TTS_PY%
  echo [WARN] Create it first:  conda create -n qwen3tts python=3.10 -y ^&^& conda activate qwen3tts ^&^& pip install -r requirements\qwen3tts.txt
  pause
  exit /b 1
)
"%QWEN3TTS_PY%" qwen3_tts_server.py
pause
