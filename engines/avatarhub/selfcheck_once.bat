@echo off
chcp 65001 > nul
title selfcheck_once
cd /d "%~dp0"
REM Load env vars (FACEFUSION_PY + service URLs SVC_*)
call "%~dp0env_config.bat"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM One-shot self-check for Task Scheduler: one pass; red flags -> alerts.py auto raise/clear.
REM Arg "noalert" = dry-run (no alerting/toast). Output overwrites logs\selfcheck_last.log (last run only, no growth).
set "ALERTFLAG=--alert"
if /i "%~1"=="noalert" set "ALERTFLAG="

"%FACEFUSION_PY%" selfcheck_pipeline.py %ALERTFLAG% > "%~dp0logs\selfcheck_last.log" 2>&1
exit /b %errorlevel%
