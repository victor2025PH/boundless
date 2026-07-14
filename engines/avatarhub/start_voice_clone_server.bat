@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

echo ============================================================
echo  Zero-Shot Voice Clone HTTP Server  (Fish-Speech)
echo  LAN: http://^<this-pc-ip^>:7855
echo ============================================================
echo.

call "%~dp0env_config.bat"

if not exist "%FISHSPEECH_PY%" (
    echo [ERROR] fishspeech env not found: %FISHSPEECH_PY%
    pause
    exit /b 1
)

set "MODEL_DIR=%BASE_DIR%\fish-speech\checkpoints\fish-speech-1.5"
if not exist "%MODEL_DIR%\model.pth" (
    echo [ERROR] Fish-Speech model missing: %MODEL_DIR%\model.pth
    pause
    exit /b 1
)

rem Allow LAN clients (run once; harmless if rule already exists)
netsh advfirewall firewall show rule name="AvatarHub Voice Clone 7855" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Adding Windows Firewall inbound rule for TCP 7855 ...
    netsh advfirewall firewall add rule name="AvatarHub Voice Clone 7855" dir=in action=allow protocol=TCP localport=7855
)

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    set "LAN_IP=%%a"
    goto :got_ip
)
:got_ip
set "LAN_IP=%LAN_IP: =%"

echo [INFO] Python : %FISHSPEECH_PY%
echo [INFO] Model  : %MODEL_DIR%
echo [INFO] Listen : 0.0.0.0:7855
echo [INFO] LAN    : http://%LAN_IP%:7855
echo [INFO] Health : http://%LAN_IP%:7855/health
echo [INFO] Clone  : POST http://%LAN_IP%:7855/v1/tts/clone
echo.

cd /d "%BASE_DIR%"
"%FISHSPEECH_PY%" "%BASE_DIR%\fish_speech_server.py"

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Server exited with code %errorlevel%.
)
pause
endlocal
