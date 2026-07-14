@echo off
chcp 65001 >nul
title FaceSwap REST API (port 8000)
call "%~dp0env_config.bat"
set "PATH=%CONDA_ROOT%\envs\facefusion;%CONDA_ROOT%\envs\facefusion\Scripts;%CONDA_ROOT%\envs\facefusion\Library\bin;%PATH%"
rem -- Phase8-1 TensorRT FP16 加速（默认关；置 1 开启，首启现场构建引擎并缓存，再启秒级）--
rem    set "FACESWAP_TRT=1"          rem 开启 TensorRT EP（换脸网 FP16）
rem    set "FACESWAP_TRT_FP16=1"     rem FP16（默认）；set 0 走 FP32
rem    set "FACESWAP_TRT_DET=0"      rem 1=检测/识别也走 TRT（首启更慢）
rem    set "FACESWAP_TRT_CACHE=%BASE_DIR%\models\trt_cache\faceswap"   rem 引擎缓存目录
echo [INFO] FaceSwap API starting on http://0.0.0.0:8000 ...
"%FACEFUSION_PY%" "%BASE_DIR%\faceswap_api.py"
pause
