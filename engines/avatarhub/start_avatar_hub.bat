@echo off
chcp 65001 > nul
title AvatarHub
cd /d "%~dp0"
rem Load env vars + secret keys (CONV_DEEPSEEK_API_KEY etc.)
call "%~dp0env_config.bat"
rem Suppress OpenMP/MKL idle spin (inherited by Hub and child services; no idle core burn)
set OMP_WAIT_POLICY=PASSIVE
set KMP_BLOCKTIME=0
rem 本地实时换脸模式：lipsync 用不到、vcam 与 realtime_stream 争 OBS 虚拟摄像头必崩，
rem 故不纳入进程守护，避免起不来→无限重拉→熔断刷屏。
set HUB_SUP_LIPSYNC=0
set HUB_SUP_VCAM=0
:loop
rem 防重复启动：若 9000 已被其它实例(如看门狗用 _launch_hub_detached.bat 拉起的)监听，
rem 则不再启动第二个 hub，等它退出再接管——杜绝两个 hub 抢 9000 端口互相打架(历史事故)。
netstat -ano | findstr ":9000" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo [%date% %time%] 检测到 9000 已在监听，等待接管中…（避免重复启动；关闭本窗口即可退出）
  timeout /t 10 /nobreak >nul
  goto loop
)
echo [%date% %time%] 启动 AvatarHub 统一控制中心...
"%FACEFUSION_PY%" avatar_hub.py
set "EC=%errorlevel%"
echo.
echo [%date% %time%] AvatarHub 已退出 (exit=%EC%)，5 秒后自动重启… 关闭本窗口即可彻底停止。
timeout /t 5 /nobreak >nul
goto loop
