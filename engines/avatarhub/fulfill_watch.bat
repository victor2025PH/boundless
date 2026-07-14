@echo off
rem Run order fulfillment once and append log (scheduled task entrypoint, every 5 min).
setlocal
cd /d "%~dp0"
"C:\Users\user\miniconda3\python.exe" fulfill_orders.py >> "%~dp0secrets\fulfill.log" 2>&1
endlocal
