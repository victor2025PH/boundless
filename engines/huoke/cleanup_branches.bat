@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ops\cleanup_branches.ps1" %*
exit /b %ERRORLEVEL%
