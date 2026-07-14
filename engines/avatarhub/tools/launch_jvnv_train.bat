@echo off
rem P8 stage-2 JVNV fusion training - fully detached (survives any parent shell).
set MASTER_ADDR=127.0.0.1
set MASTER_PORT=10087
set RANK=0
set LOCAL_RANK=0
set WORLD_SIZE=1
cd /d C:\SBV2
"%USERPROFILE%\miniconda3\envs\sbv2\python.exe" train_ms_jp_extra.py -c "Data\LinXiaolingJVNV\config.json" -m "Data\LinXiaolingJVNV" --assets_root "C:\SBV2\model_assets" --skip_default_style --speedup >> "C:\SBV2\Data\LinXiaolingJVNV\train_live.log" 2>&1
