@echo off
chcp 65001 >nul
title Chrome Telegram Web (DirectShow / OBS)
rem Chrome 149+ removed chrome://flags Media Foundation toggle.
rem Force DirectShow so OBS Virtual Camera appears in browser getUserMedia.
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo Chrome not found: %CHROME%
  pause
  exit /b 1
)
echo Launching Chrome with DirectShow camera (OBS should appear in device list)...
echo URL: https://web.telegram.org/k/
start "" "%CHROME%" --disable-features=MediaFoundationVideoCapture "https://web.telegram.org/k/"
exit /b 0
