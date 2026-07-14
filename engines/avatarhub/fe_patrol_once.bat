@echo off
chcp 65001 > nul
title fe_patrol_once
cd /d "%~dp0"
REM Load env vars (FACEFUSION_PY + ACCEPT_HUB + service URLs)
call "%~dp0env_config.bat"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not defined ACCEPT_HUB set ACCEPT_HUB=http://127.0.0.1:9000

REM Daily frontend patrol: fe_smoke + fe_interact; failure -> alerts.py (--alert).
REM Arg "noalert" = dry-run. Output -> logs\fe_patrol_last.log (overwrite each run).
set "ALERTFLAG=--alert"
if /i "%~1"=="noalert" set "ALERTFLAG="

"%FACEFUSION_PY%" -X utf8 tools\_fe_patrol.py %ALERTFLAG% > "%~dp0logs\fe_patrol_last.log" 2>&1
exit /b %errorlevel%
