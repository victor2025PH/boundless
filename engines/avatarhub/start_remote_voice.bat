@echo off
chcp 65001 >nul
title 远端语音节点 (4090): Fish-Speech 克隆音 + STT 识别

rem ============================================================
rem  Run on the [4090 machine]: expose clone-voice(7855)+STT(7854) to the LAN,
rem  offloading the 5090's compute/VRAM and removing single-GPU contention stalls/queueing.
rem
rem  Prerequisites (prepare on this 4090 machine first):
rem    1) fishspeech / cosytts conda envs installed (same as 5090)
rem    2) corresponding model files placed (fish-speech-1.5 etc.)
rem    3) this script sits in the same dir as fish_speech_server.py / stt_server.py
rem ============================================================

rem Suppress idle spin (same as host); no idle CPU burn
set OMP_WAIT_POLICY=PASSIVE
set KMP_BLOCKTIME=0
set BASE_DIR=%~dp0

rem vvv adjust to this machine's actual conda install paths vvv
set FISH_PY=C:\Users\%USERNAME%\Miniconda3\envs\fishspeech\python.exe
set STT_PY=C:\Users\%USERNAME%\Miniconda3\envs\cosytts\python.exe

echo [1/2] 启动 Fish-Speech 克隆音 (监听 0.0.0.0:7855)...
start "RemoteFishTTS" /MIN cmd /k "chcp 65001 >nul && "%FISH_PY%" "%BASE_DIR%fish_speech_server.py""

echo [2/2] 启动 STT 语音识别 (监听 0.0.0.0:7854)...
start "RemoteSTT" /MIN cmd /k "chcp 65001 >nul && "%STT_PY%" "%BASE_DIR%stt_server.py""

echo.
echo ============================================================
echo  已启动。请记下【本机局域网 IP】(ipconfig 查看)，
echo  然后在【5090 主机】的 env_config.bat 中加入：
echo     set SVC_FISH_TTS=http://本机IP:7855
echo     set SVC_STT=http://本机IP:7854
echo  再重启主机 AvatarHub。主机守护会自动「跳过本地语音、改用远端」。
echo ============================================================
pause
