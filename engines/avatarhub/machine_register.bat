@echo off
REM machine_register.bat — 向官网后台登记本机（供计划任务 AvatarHub_MachineReg 隐藏调用，单次）。
REM 适合不跑 Hub/换脸等"已内置登记"服务的机器（如 STT、口型分机）。登记的是机器管理信息(指纹/显卡/版本)，
REM 非内容；可 AVATARHUB_ADMIN_REGISTER=0 关。输出写 logs\machine_register.log。
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs

set "PY=%AVH_PY%"
if "%PY%"=="" set "PY=C:\Users\user\Miniconda3\envs\facefusion\python.exe"
if not exist "%PY%" set "PY=python"

echo [%date% %time%] 登记本机 (PY=%PY%) >> "logs\machine_register.log"
"%PY%" admin_client.py >> "logs\machine_register.log" 2>&1
echo [%date% %time%] 完成(码=%errorlevel%) >> "logs\machine_register.log"
exit /b %errorlevel%
