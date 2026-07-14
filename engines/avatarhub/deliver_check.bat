@echo off
chcp 65001 >nul
title 一键交付自检
rem -- One-click delivery self-check: offline preflight -> Hub probe -> online check -> regression gate (incl. streaming) -> quick gains --
rem    Go down the checklist before launch. Args pass through to deliver_check.py (e.g. --full / --start / --only).
rem    Full regression includes browser E2E (needs playwright in facefusion env): if missing, this script asks to set it up.
rem    Non-interactive/CI: set AVATARHUB_NOPROVISION=1 to skip the prompt (missing items auto-marked SKIP, do not block deliverability).
call "%~dp0env_config.bat"
if not defined FACEFUSION_PY set "FACEFUSION_PY=python"
if not defined BASE_DIR set "BASE_DIR=%~dp0"

if defined AVATARHUB_NOPROVISION goto run
"%FACEFUSION_PY%" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('playwright') else 3)" >nul 2>&1
if not errorlevel 1 goto run
echo.
echo [提示] 完整回归的浏览器 E2E 需要 playwright（facefusion 环境），当前未安装。
echo        直接继续也可以：未装的项会自动标为 SKIP，不阻断"可交付"结论。
set "ANS=N"
set /p "ANS=Auto-provision now (playwright + chromium)? [y/N]: "
if /i "%ANS%"=="y" "%FACEFUSION_PY%" "%BASE_DIR%\provision.py" --with-selfcheck

:run
"%FACEFUSION_PY%" "%BASE_DIR%\deliver_check.py" %*
echo.
echo 退出码 %errorlevel%  (0=可交付 1=有警告 2=不可交付)
pause
