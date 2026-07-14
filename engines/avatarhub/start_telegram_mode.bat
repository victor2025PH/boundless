@echo off
chcp 65001 >nul
title Telegram Call Mode (faceswap + translate-voice + subtitle)
rem ASCII-only launcher: load env, then hand off to the Python orchestrator
rem (cmd batch parses UTF-8/CJK unreliably; Python does the real work).
call "%~dp0env_config.bat"
"%FACEFUSION_PY%" "%~dp0start_telegram_mode.py" %*
echo.
pause
