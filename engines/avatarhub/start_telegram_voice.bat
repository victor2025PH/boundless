@echo off
chcp 65001 >nul
title Telegram 实时翻译克隆声 - 一键启动
echo 正在启动出向克隆声链路(手机麦 - 翻译 - 克隆声 - Telegram麦克风)...
echo.
"C:\Users\user\Miniconda3\envs\facefusion\python.exe" "%~dp0start_telegram_voice.py" %*
echo.
echo 完成。Telegram 麦克风请选 "CABLE Output (VB-Audio Virtual Cable)"。
pause
