@echo off
chcp 65001 >nul
rem 登录即拉起内存/存活看门狗（无需管理员）。带去重：已在跑则不重复启动。
powershell -NoProfile -ExecutionPolicy Bypass -Command "if(-not (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*mem_watchdog.py*' })){ Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','C:\模仿音色\start_mem_watchdog.bat' -WindowStyle Hidden }"
