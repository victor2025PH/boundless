@echo off
chcp 65001 >nul
title Look Pack 定妆包一键激活
rem 双击即可：直播中会自动拒绝执行（避让闸），下播后再点。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\apply_look_pack_update.ps1" %*
pause
