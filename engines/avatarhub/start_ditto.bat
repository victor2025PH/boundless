@echo off
chcp 65001 >nul
title Ditto realtime full-face lipsync (port 8096)
rem Ditto is a STANDALONE external service (own repo C:\ditto + own conda env). Do NOT
rem source the main env_config.bat. Keep this launcher minimal: no multi-line if-blocks
rem (cmd mis-parses them when invoked via "cmd /c" from Start-Process, skipping lines).
if not defined CONDA_ROOT set "CONDA_ROOT=%USERPROFILE%\Miniconda3"
if not defined DITTO_DIR set "DITTO_DIR=C:\ditto"
set "DITTO_PY=%CONDA_ROOT%\envs\ditto\python.exe"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set OMP_WAIT_POLICY=PASSIVE
set KMP_BLOCKTIME=0
cd /d "%DITTO_DIR%"
echo [INFO] Ditto realtime full-face HD lipsync on http://0.0.0.0:8096 (RTF~1.0)
echo [INFO] python: %DITTO_PY%
echo [INFO] logs:   %DITTO_DIR%\_ditto_out.log  (tail for progress/errors)
echo [INFO] AVATARHUB_LIPSYNC_RT_DEFAULT=ditto (deploy.env.bat) = realtime-HD default.
rem Redirect to a file so it runs in any launch mode (a console-less parent leaves invalid
rem stdout handles and ditto's verbose output would otherwise crash on first write).
"%DITTO_PY%" ditto_server.py >"_ditto_out.log" 2>&1
echo [exit] ditto stopped; see %DITTO_DIR%\_ditto_out.log
pause
