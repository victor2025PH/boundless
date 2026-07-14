@echo off
REM sign_worker_watch.bat — 签发机启动器（供计划任务 AvatarHub_SignWorker 隐藏调用，跑一次；
REM 崩溃退出由计划任务的重复触发+IgnoreNew 在 ~2 分钟内自愈，无需自循环）。
REM 私钥只在本机；--watch 20 内部每 20 秒轮询官网签发队列就地签发。日志滚动写 logs\sign_worker.log。
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs

set "PY=%AVH_PY%"
if "%PY%"=="" set "PY=C:\Users\user\Miniconda3\envs\facefusion\python.exe"
if not exist "%PY%" set "PY=python"

echo [%date% %time%] 启动签发机 --watch 20 (PY=%PY%) >> "logs\sign_worker.log"
"%PY%" -u sign_worker.py --watch 20 >> "logs\sign_worker.log" 2>&1
echo [%date% %time%] 进程退出(码=%errorlevel%)，计划任务将自动重启 >> "logs\sign_worker.log"
exit /b %errorlevel%
