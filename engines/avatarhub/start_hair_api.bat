@echo off

chcp 65001 >nul

title Hair Transfer API

call "%~dp0env_config.bat"

set "PATH=%CONDA_ROOT%\envs\facefusion\Scripts;%CONDA_ROOT%\envs\facefusion;%PATH%"

"%FACEFUSION_PY%" "%BASE_DIR%\hair_api.py"

pause

