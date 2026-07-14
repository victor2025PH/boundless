@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ops\migrate.ps1" %*
exit /b %ERRORLEVEL%
