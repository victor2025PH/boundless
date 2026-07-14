@echo off
chcp 65001 >nul
rem P5d LinXiaoling JP-Extra 训练启动器（sbv2 env · cu128 5090）
set PY=%USERPROFILE%\miniconda3\envs\sbv2\python.exe
if not exist "%PY%" set PY=%AVATARHUB_PY_COSYTTS%
if not exist "%PY%" (
  echo [错误] cosytts Python 未找到
  exit /b 1
)
set SBV2_ROOT=C:\SBV2
set DATA=%SBV2_ROOT%\Data\LinXiaoling_JP
cd /d %SBV2_ROOT%
set MASTER_ADDR=127.0.0.1
set MASTER_PORT=10086
set RANK=0
set LOCAL_RANK=0
set WORLD_SIZE=1
echo [SBV2] 训练 LinXiaoling_JP JP-Extra epochs=100
"%PY%" train_ms_jp_extra.py -c "%DATA%\config.json" -m "%DATA%" --assets_root "%SBV2_ROOT%\model_assets" --skip_default_style --speedup
exit /b %ERRORLEVEL%
