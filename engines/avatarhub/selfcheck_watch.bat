@echo off
chcp 65001 > nul
title 全链路巡检 selfcheck_watch
cd /d "%~dp0"
rem 加载环境变量(含 FACEFUSION_PY / 各服务地址)
call "%~dp0env_config.bat"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem 巡检间隔秒数：第一个参数可覆盖，默认 120s
set "INTERVAL=%~1"
if "%INTERVAL%"=="" set "INTERVAL=120"

rem 第二个参数 noalert = 干跑(不联动 alerts.py，不弹窗/不写告警态)，用于试跑/演示
set "ALERTFLAG=--alert"
set "ALERTDESC=+ 自动告警(--alert)"
if /i "%~2"=="noalert" (
  set "ALERTFLAG="
  set "ALERTDESC=(干跑·不联动告警)"
)

echo [%date% %time%] 启动全链路常态巡检：每 %INTERVAL%s 一轮 %ALERTDESC%
echo   红旗(换脸掉CPU/核心离线/阶段偏慢)会经 alerts.py 自动 raise/clear，落 logs\alerts.jsonl
echo   用法: selfcheck_watch.bat [间隔秒=120] [noalert]     关闭本窗口即停止。
echo.
"%FACEFUSION_PY%" selfcheck_pipeline.py --watch %INTERVAL% %ALERTFLAG%
