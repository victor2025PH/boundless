@echo off
chcp 65001 >nul
title Chrome Webcam Test (DirectShow / OBS)
rem Verify OBS Virtual Camera before Telegram Web call.
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo Chrome not found: %CHROME%
  pause
  exit /b 1
)
echo Launching Chrome with DirectShow camera for webcam test...
start "" "%CHROME%" --disable-features=MediaFoundationVideoCapture "https://webcamtests.com/"
exit /b 0
