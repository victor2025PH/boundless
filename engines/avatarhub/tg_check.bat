@echo off
chcp 65001 >nul
title Telegram Mode Preflight (readiness check)
rem ASCII-only launcher; real work in start_telegram_mode.py --check (read-only).
call "%~dp0env_config.bat"
"%FACEFUSION_PY%" "%~dp0start_telegram_mode.py" --check %*
echo.
pause
