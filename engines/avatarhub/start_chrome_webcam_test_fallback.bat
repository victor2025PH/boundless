@echo off
chcp 65001 >nul
title Chrome Webcam Test (DirectShow + no GPU accel)
rem Fallback if start_chrome_webcam_test.bat still shows only iVCam.
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo Chrome not found: %CHROME%
  pause
  exit /b 1
)
echo Launching Chrome: DirectShow + disable GPU (fallback)...
start "" "%CHROME%" --disable-features=MediaFoundationVideoCapture --disable-gpu "https://webcamtests.com/"
exit /b 0
