@echo off
chcp 65001 >nul
title AvatarHub 首次运行向导
cd /d "%~dp0"
call "%~dp0env_config.bat"

echo ============== 开机前预检 ==============
"%FACEFUSION_PY%" "%~dp0doctor.py" --preflight
echo =======================================
echo.
echo 即将打开图形化配置向导（需要 Hub 在运行）。
echo   未启动 Hub 时：先运行 start_all_services.bat，再回来打开本页。
echo   向导地址： http://127.0.0.1:9000/setup
echo.
start "" "http://127.0.0.1:9000/setup"
pause
