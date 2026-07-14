@echo off
chcp 65001 >nul
title DFM 角色库 Lab (port 8005)
call "%~dp0env_config.bat"
set "PATH=%CONDA_ROOT%\envs\facefusion;%CONDA_ROOT%\envs\facefusion\Scripts;%CONDA_ROOT%\envs\facefusion\Library\bin;%PATH%"
rem -- 默认 CPU 推理(不抢生产显存)；置 1 走 GPU（单次更快，但会占用 5090）--
rem    set "DFM_LAB_GPU=1"
echo [INFO] DFM 角色库 Lab starting on http://127.0.0.1:8005/ui ...
"%FACEFUSION_PY%" "%BASE_DIR%\dfm_lab_server.py"
pause
