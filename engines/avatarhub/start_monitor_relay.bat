@echo off
chcp 65001 > nul
title MonitorRelay
echo 启动 手机监听中继 (PC音频+字幕 -> 手机)...
cd /d "%~dp0"
call "%~dp0env_config.bat"
set OMP_WAIT_POLICY=PASSIVE
set KMP_BLOCKTIME=0
"%FACEFUSION_PY%" monitor_relay.py
pause
