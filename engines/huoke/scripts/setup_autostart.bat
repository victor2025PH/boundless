@echo off
chcp 65001 >nul
:: ═══════════════════════════════════════════════════
:: OpenClaw Worker 开机自启动配置
:: 运行一次即可，会创建 Windows 计划任务
:: ═══════════════════════════════════════════════════

set "PROJECT_DIR=%~dp0.."
set "PYTHON_EXE=python"

:: 检测Python路径
where python >nul 2>&1
if %errorLevel% neq 0 (
    for %%p in (
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) do (
        if exist %%~p (
            set "PYTHON_EXE=%%~p"
            goto :found
        )
    )
    echo Python 未找到！
    pause
    exit /b 1
)
:found

echo.
echo  ╔══════════════════════════════════════╗
echo  ║  OpenClaw Worker 自启动配置          ║
echo  ╚══════════════════════════════════════╝
echo.
echo  Python: %PYTHON_EXE%
echo  项目:   %PROJECT_DIR%
echo.

:: 删除旧的计划任务（如果存在）
schtasks /delete /tn "OpenClaw-Worker" /f >nul 2>&1

:: 创建计划任务：用户登录时自动启动
schtasks /create /tn "OpenClaw-Worker" ^
  /tr "\"%PYTHON_EXE%\" \"%PROJECT_DIR%\service_wrapper.py\"" ^
  /sc onlogon ^
  /rl highest ^
  /f

if %errorLevel% == 0 (
    echo.
    echo  ✅ 自启动配置成功！
    echo.
    echo  任务名称: OpenClaw-Worker
    echo  触发条件: 用户登录时自动启动
    echo  启动命令: %PYTHON_EXE% service_wrapper.py
    echo.
    echo  管理方式:
    echo    查看: schtasks /query /tn "OpenClaw-Worker"
    echo    删除: schtasks /delete /tn "OpenClaw-Worker" /f
    echo    手动运行: schtasks /run /tn "OpenClaw-Worker"
) else (
    echo.
    echo  ❌ 配置失败，请以管理员身份运行此脚本
)
echo.
pause
