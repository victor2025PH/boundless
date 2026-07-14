@echo off

chcp 65001 >nul

title Video Try-On API (CatV2TON)

call "%~dp0env_config.bat"

rem CatV2TON 动态试衣（视频虚拟试衣，端口 8006）。与 FitDiT 共用 fitdit 环境。
rem 模型懒加载（启动秒起），首单 ~45s 冷启动；空闲 10min 自动整体卸载。
rem 显存硬顶=物理-4G：越界抛 OOM 报错，绝不溢出共享内存拖死整机（阶段15 事故教训）。
set "VIDEOTRYON_PY=%CONDA_ROOT%\envs\fitdit\python.exe"
if not exist "%VIDEOTRYON_PY%" set "VIDEOTRYON_PY=%FACEFUSION_PY%"

"%VIDEOTRYON_PY%" "%BASE_DIR%\videotryon_api.py"

pause
