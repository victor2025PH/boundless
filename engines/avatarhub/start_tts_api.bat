@echo off
chcp 65001 >nul
title TTS API (XTTS-v2, port 7851)
set "COQUI_TOS_AGREED=1"
set "CONDA_PREFIX=C:\alltalk_env"
set "PATH=C:\alltalk_env\Scripts;C:\alltalk_env;C:\ffmpeg\bin;%PATH%"
echo [INFO] TTS API starting on http://0.0.0.0:7851
echo [INFO] First run will download XTTS-v2 model (~2GB), please wait...
C:\alltalk_env\python.exe "%~dp0tts_api.py"
pause
