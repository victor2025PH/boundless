@echo off
REM AvatarHub test gate one-click entry (de-facto CI)
REM   gate.bat            offline gate (syntax compile + offline unit tests), common dev loop
REM   gate.bat --online   add online full check (start Hub via start_all_services.bat first)
REM   gate.bat --compile-only   syntax compile only (fastest)
setlocal
cd /d "%~dp0"
python gate.py %*
exit /b %ERRORLEVEL%
