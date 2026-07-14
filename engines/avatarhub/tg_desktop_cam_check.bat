@echo off
chcp 65001 >nul
title Telegram Desktop Camera Check (MF)
call "%~dp0env_config.bat" 2>nul
"%FACEFUSION_PY%" "%~dp0tg_desktop_cam_check.py"
echo.
pause
