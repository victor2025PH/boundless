@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ops\sync_with_main.ps1" %*
exit /b %ERRORLEVEL%
