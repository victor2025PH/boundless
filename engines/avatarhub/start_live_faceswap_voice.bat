@echo off
chcp 65001 >nul
title 真人实时换脸+变声（视频通话）

rem ============================================================
rem  Pipeline B: you on camera -> realtime faceswap (video) + realtime voice change (audio) -> WeChat/Zoom/Douyin
rem  Video: camera -> faceswap_api(8000,CUDA) -> realtime_stream -> OBS virtual camera
rem  Audio: mic -> RVC realtime voice change (gui_v1) -> VB-Cable virtual mic
rem
rem  Usage: start_live_faceswap_voice.bat [camera index]   (default 0)
rem ============================================================

call "%~dp0env_config.bat"

set CAM=%1
if "%CAM%"=="" set CAM=0

set RVC_DIR=%BASE_DIR%\Retrieval-based-Voice-Conversion-WebUI

echo [清理] 停止旧的换脸进程...
taskkill /F /FI "WINDOWTITLE eq FaceSwap-API*"   >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq RealTime-Swap*"  >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq RVC-Realtime*"   >nul 2>&1

echo.
echo ============================================
echo  前置检查（一次性安装，缺则先装）
echo ============================================
echo   1) OBS Studio → 工具 → 虚拟摄像头 → 启动   （视频输出到 APP 必需）
echo   2) VB-CABLE 虚拟声卡                        （变声输出到 APP 必需）
echo.

echo [1/3] 启动换脸引擎 FaceSwap-API（端口 8000，facefusion 环境 / CUDA）...
start "FaceSwap-API" /MIN cmd /k "chcp 65001 >nul && "%FACEFUSION_PY%" "%BASE_DIR%\faceswap_api.py""

echo     等待换脸引擎加载模型（最多 ~90 秒）...
set /a _tries=0
:WAIT_SWAP
set /a _tries+=1
timeout /t 3 /nobreak >nul
curl.exe -s -o nul -w "%%{http_code}" http://127.0.0.1:8000/health 2>nul | findstr "200" >nul
if %errorlevel%==0 goto SWAP_OK
if %_tries% GEQ 30 (
    echo     [警告] 换脸引擎 90s 内未就绪，仍继续（画面可能先显示原始帧）。
    goto SWAP_OK
)
goto WAIT_SWAP
:SWAP_OK
echo     换脸引擎就绪 ✓

echo.
echo [2/3] 启动实时换脸推流（摄像头 %CAM% → OBS 虚拟摄像头，直连 8000）...
rem SWAP_API_URL connects straight to faceswap_api; no need to start the whole avatar Hub
start "RealTime-Swap" cmd /k "chcp 65001 >nul && set SWAP_API_URL=http://127.0.0.1:8000/faceswap && "%FACEFUSION_PY%" "%BASE_DIR%\realtime_stream.py" --source %CAM% --width 1280 --height 720"

echo.
echo [3/3] 启动 RVC 实时变声界面（rvc 环境，cu128/5090 已修兼容）...
start "RVC-Realtime" /d "%RVC_DIR%" cmd /k "chcp 65001 >nul && "%RVC_PY%" gui_v1.py"

echo.
echo ============================================
echo  启动完成 —— 接下来在各窗口里操作：
echo ============================================
echo  【选脸】要换成谁：浏览器开 http://127.0.0.1:8000/faces 看可用脸；
echo         切换： curl "http://127.0.0.1:8000/set_face?name=刘德华"
echo         （把图片放进 %BASE_DIR%\faces 即新增可选脸）
echo.
echo  【变声】RVC 窗口里：输入设备=你的麦克风；输出设备=CABLE Input；
echo         选一个 .pth 音色模型 → 点「开始音频转换」。
echo.
echo  【接入视频 APP】（微信/Zoom/抖音/腾讯会议）：
echo         摄像头 → 选「OBS Virtual Camera」
echo         麦克风 → 选「CABLE Output (VB-Audio Virtual Cable)」
echo ============================================
echo.
echo 关闭本窗口不影响已启动的服务。各服务窗口按 Ctrl+C 或关闭即停止。
pause
