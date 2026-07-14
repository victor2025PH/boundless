@echo off
chcp 65001 >nul
title Stop Telegram Call Mode
rem ASCII-only launcher; real work in stop_telegram_mode.py (UTF-8 safe).
rem Skip LAN service discovery here (we are only stopping local processes).
set "AVATARHUB_NO_SERVICE_DISCOVERY=1"
call "%~dp0env_config.bat" >nul 2>&1
"%FACEFUSION_PY%" "%~dp0stop_telegram_mode.py" %*
echo.
pause
