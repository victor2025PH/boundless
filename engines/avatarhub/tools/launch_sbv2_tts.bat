@echo off
chcp 65001 >nul
rem P5d SBV2 JP-Extra 推理服务（sbv2 env · cu128 5090）
set PY=%USERPROFILE%\miniconda3\envs\sbv2\python.exe
if not exist "%PY%" set PY=%AVATARHUB_PY_COSYTTS%
cd /d %~dp0..
set SBV2_ROOT=C:\SBV2
set SBV2_MODEL_DIR=C:\SBV2\Data\LinXiaoling_JP
set SBV2_TTS_PORT=7861
set SBV2_DEVICE=cuda:0
echo [SBV2TTS] 启动端口 %SBV2_TTS_PORT% 模型 %SBV2_MODEL_DIR%
"%PY%" sbv2_tts_server.py
exit /b %ERRORLEVEL%
