@echo off
rem 每日视频发布计划任务入口：先尝试 API 生成（未配 key 自动跳过），再发布一条。
setlocal
cd /d "%~dp0"
"C:\Users\user\miniconda3\python.exe" generate_daily.py >> "%~dp0..\secrets\publish.log" 2>&1
"C:\Users\user\miniconda3\python.exe" publish_daily.py >> "%~dp0..\secrets\publish.log" 2>&1
endlocal
