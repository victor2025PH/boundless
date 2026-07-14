@echo off
chcp 65001 >nul
title Edge Telegram Web (DirectShow / OBS)
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" (
  echo Edge not found: %EDGE%
  pause
  exit /b 1
)
echo Launching Edge with DirectShow camera (OBS should appear in device list)...
start "" "%EDGE%" --disable-features=MediaFoundationVideoCapture "https://web.telegram.org/k/"
exit /b 0
