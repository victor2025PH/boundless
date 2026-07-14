@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem === 监听桥：把直播用的克隆语音(CABLE Output)实时送到蓝牙耳机(EDIFIER) ===
rem 先关掉已在运行的监听桥，避免重复播放(路径从 %~dp0 推导,项目搬家零改动)
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter 'Name=''python.exe''' | Where-Object { $_.CommandLine -like '*monitor_bridge.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; $env:PYTHONIOENCODING='utf-8'; $b=(Get-Location).Path; Start-Process -FilePath 'C:\Users\user\Miniconda3\envs\facefusion\python.exe' -ArgumentList 'monitor_bridge.py' -WorkingDirectory $b -WindowStyle Hidden -RedirectStandardOutput (Join-Path $b 'logs\monitor_bridge.log') -RedirectStandardError (Join-Path $b 'logs\monitor_bridge.err.log')"
echo.
echo [OK] 监听桥已后台启动。
echo      戴上 / 唤醒 EDIFIER 耳机后，会自动开始监听克隆语音。
echo      日志: logs\monitor_bridge.log
echo      停止: 结束名称含 monitor_bridge.py 的 python 进程即可。
echo.
pause
