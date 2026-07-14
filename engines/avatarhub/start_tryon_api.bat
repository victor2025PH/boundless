@echo off

chcp 65001 >nul

title Virtual Try-On API (FitDiT)

call "%~dp0env_config.bat"

rem FitDiT 宿主环境（克隆自 musethepeak：diffusers 0.38 + torch 2.11+cu128 + ort-gpu）。
rem env 缺失时回退 facefusion（那边没 diffusers，会走 503——但服务面/健康检查仍在）。
set "TRYON_PY=%CONDA_ROOT%\envs\fitdit\python.exe"
if not exist "%TRYON_PY%" set "TRYON_PY=%FACEFUSION_PY%"

rem FITDIT_OFFLOAD=1(默认) 峰值显存 <6G，直播时也能跑；空闲机可 set FITDIT_OFFLOAD=0 提速
"%TRYON_PY%" "%BASE_DIR%\tryon_api.py"

pause
