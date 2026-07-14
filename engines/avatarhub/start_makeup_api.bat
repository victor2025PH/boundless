@echo off

chcp 65001 >nul

title Makeup Transfer API

call "%~dp0env_config.bat"

set "PATH=%CONDA_ROOT%\envs\facefusion\Scripts;%CONDA_ROOT%\envs\facefusion;%PATH%"

"%FACEFUSION_PY%" "%BASE_DIR%\makeup_api.py"

pause
