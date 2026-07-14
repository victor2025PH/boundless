@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ops\status.ps1" %*
exit /b %ERRORLEVEL%
