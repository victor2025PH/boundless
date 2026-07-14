@echo off
chcp 65001 >nul
title 启动核心服务

rem Load environment variables
call "%~dp0env_config.bat"

rem -- Suppress PyTorch/OpenMP idle spin (heavy resident services burn cores when idle -> global stutter) --
rem Child procs inherit these via start; idle threads sleep instead of busy-wait, idle CPU near 0
set OMP_WAIT_POLICY=PASSIVE
set KMP_BLOCKTIME=0

rem -- Whether to also start optional extra services (faceswap/hair/Coqui-TTS/emotion-TTS/singing/HD-lip/enhance) --
rem Default runs only the realtime conversation core, leaving VRAM/CPU for the desktop. For extras: set START_EXTRAS=1 then run.
if not defined START_EXTRAS set START_EXTRAS=0

rem -- Pre-boot offline preflight (opt-in): on new/changed machines set PREFLIGHT=1 to check GPU/conda/ports/multi-GPU replicas.
rem    Off by default -> zero change to existing startup. On crit (exit code 2) it warns but does not force-abort.
if not defined PREFLIGHT set PREFLIGHT=0
if "%PREFLIGHT%"=="1" (
    echo ============== 开机前预检（doctor --preflight）==============
    "%FACEFUSION_PY%" "%BASE_DIR%\doctor.py" --preflight
    if errorlevel 2 (
        echo [预检] 发现严重问题（见上）。仍要继续启动请按任意键，或 Ctrl+C 中止修复。
        pause >nul
    )
    echo ============================================================
)

echo [清理] 停止旧进程...
taskkill /F /FI "WINDOWTITLE eq FaceSwap-API*"  >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq TTS-API*"       >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Hair-API*"      >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq AvatarHub*"     >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq LipSync*"       >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq EmotionTTS*"    >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Singing*"       >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Enhance*"       >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq VCam*"          >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq LatentSync*"    >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq EchoMimic*"     >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Ditto*"         >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq STT*"           >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq FishTTS*"       >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq VoxCPM*"        >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq NemoSTT*"       >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq VideoTryOn*"    >nul 2>&1

echo ============================================
echo  核心链路（实时对话）：FishTTS / STT / LipSync / VCam / Hub
echo ============================================

if defined SVC_FISH_TTS (
    echo [核心 1/5] 克隆音 Fish-Speech 已分担到远端 %SVC_FISH_TTS%（跳过本地启动，省 5090 显存）
) else (
    echo [核心 1/5] 启动 克隆音 Fish-Speech（端口 7855，fishspeech 环境）...
    start "FishTTS" /MIN cmd /k "chcp 65001 >nul && "%FISHSPEECH_PY%" "%BASE_DIR%\fish_speech_server.py""
)

if defined SVC_STT (
    echo [核心 2/5] STT 语音转文字 已分担到远端 %SVC_STT%（跳过本地启动，省 5090 显存）
) else (
    echo [核心 2/5] 启动 STT 语音转文字（端口 7854，cosytts 环境）...
    start "STT" /MIN cmd /k "chcp 65001 >nul && "%COSYTTS_PY%" "%BASE_DIR%\stt_server.py""
)

echo [核心 3/5] 启动 LipSync 口型同步（端口 8090，musethepeak 环境）...
start "LipSync" /MIN cmd /k "chcp 65001 >nul && "%MUSETHEPEAK_PY%" "%BASE_DIR%\lipsync_server.py""

echo [核心 4/5] 启动 数字人广播中枢 OBS+WebRTC（端口 7870，facefusion 环境）...
start "VCam" /MIN cmd /k "chcp 65001 >nul && "%FACEFUSION_PY%" "%BASE_DIR%\vcam_server.py""

echo [核心 5/5] 启动 AvatarHub 统一控制中心（端口 9000）...
start "AvatarHub" /MIN cmd /k "chcp 65001 >nul && "%FACEFUSION_PY%" "%BASE_DIR%\avatar_hub.py""

if "%START_EXTRAS%"=="1" (
    echo ============================================
    echo  扩展服务 START_EXTRAS=1：换脸/发型/Coqui-TTS/情感TTS/唱歌/高清口型/增强
    echo ============================================
    if defined SVC_FACESWAP (
        echo [扩展] 换脸 已分担到远端 %SVC_FACESWAP%（跳过本地启动，省 5090 显存）
    ) else (
        echo [扩展] 换脸服务（端口 8000）...
        start "FaceSwap-API" /MIN cmd /k "chcp 65001 >nul && "%FACEFUSION_PY%" "%BASE_DIR%\faceswap_api.py""
    )
    echo [扩展] Coqui-TTS（端口 7851）...
    start "TTS-API" /MIN cmd /k "chcp 65001 >nul && set COQUI_TOS_AGREED=1 && "%RVC_PY%" "%BASE_DIR%\tts_api.py""
    echo [扩展] 发型服务（端口 8001）...
    start "Hair-API" /MIN cmd /k "chcp 65001 >nul && "%FACEFUSION_PY%" "%BASE_DIR%\hair_api.py""
    start "Makeup-API" /MIN cmd /k "chcp 65001 >nul && "%FACEFUSION_PY%" "%BASE_DIR%\makeup_api.py""
    echo [扩展] 虚拟试衣 FitDiT（端口 8002，独立 fitdit 环境，offload 显存 ^<6G）...
    start "TryOn-API" /MIN cmd /c ""%BASE_DIR%\start_tryon_api.bat""
    echo [扩展] 动态试衣 CatV2TON 视频（端口 8006，fitdit 环境，懒加载+空闲自卸）...
    start "VideoTryOn-API" /MIN cmd /c ""%BASE_DIR%\start_videotryon_api.bat""
    echo [扩展] 人脸增强 GFPGAN（端口 8092）...
    start "Enhance" /MIN cmd /k "chcp 65001 >nul && "%FACEFUSION_PY%" "%BASE_DIR%\enhance_server.py""
    echo [扩展] LatentSync 高清口型 512（端口 8091，按需加载+空闲卸载）...
    start "LatentSync" /MIN cmd /k "chcp 65001 >nul && set LATENTSYNC_PRELOAD=0 && set LATENTSYNC_IDLE_UNLOAD=120 && "%LATENTSYNC_PY%" "%BASE_DIR%\latentsync_server.py""
    if exist "%ECHOMIMIC_DIR%\echomimic_server.py" (
        echo [扩展] EchoMimic 全脸高清数字人 512（端口 8095，echomimic 环境，离线出片）...
        start "EchoMimic" /MIN /D "%ECHOMIMIC_DIR%" cmd /k "chcp 65001 >nul && "%ECHOMIMIC_PY%" "%ECHOMIMIC_DIR%\echomimic_server.py""
    ) else (
        echo [扩展] 跳过 EchoMimic（未找到 %ECHOMIMIC_DIR%\echomimic_server.py）
    )
    if exist "%DITTO_DIR%\ditto_server.py" (
        echo [扩展] Ditto 实时全脸说话头 512（端口 8096，ditto 环境，warm 后 50-60fps 快于实时）...
        start "Ditto" /MIN /D "%DITTO_DIR%" cmd /k "chcp 65001 >nul && "%DITTO_PY%" "%DITTO_DIR%\ditto_server.py""
    ) else (
        echo [扩展] 跳过 Ditto（未找到 %DITTO_DIR%\ditto_server.py）
    )
    if defined SVC_EMOTION_TTS (
        echo [扩展] 情感TTS 已分担到远端 %SVC_EMOTION_TTS%（跳过本地启动，省 5090 显存）
    ) else (
        echo [扩展] EmotionTTS 情感语音（端口 7852）...
        start "EmotionTTS" /MIN cmd /k "chcp 65001 >nul && "%COSYTTS_PY%" "%BASE_DIR%\emotion_tts_server.py""
    )
    rem Song-P1: 7853 由 GPT-SoVITS(运行时已清空) 切换为 Song Studio AI 翻唱（YingMusic-SVC, ymsvc 环境）
    if exist "%YMSVC_PY%" (
        echo [扩展] Song Studio AI 翻唱（端口 7853, ymsvc 环境）...
        start "SongStudio" /MIN cmd /k "chcp 65001 >nul && "%YMSVC_PY%" "%BASE_DIR%\song_studio_server.py""
    ) else (
        echo [跳过] Song Studio：ymsvc 环境不存在（tools\setup_song_studio.py 部署后可用）
    )
    if exist "%VOXCPM_PY%" (
        echo [扩展] VoxCPM2 可商用克隆 TTS（端口 7856，voxcpm 环境）...
        start "VoxCPM" /MIN cmd /k "chcp 65001 >nul && "%VOXCPM_PY%" "%BASE_DIR%\voxcpm_server.py""
    ) else (
        echo [扩展] 跳过 VoxCPM2（未找到 voxcpm 环境，python provision.py --create --only voxcpm 可创建）
    )
    if exist "%NEMOASR_PY%" (
        echo [扩展] Nemotron3.5 流式 STT（端口 7857，nemoasr 环境）...
        start "NemoSTT" /MIN cmd /k "chcp 65001 >nul && "%NEMOASR_PY%" "%BASE_DIR%\nemotron_stt_server.py""
    ) else (
        echo [扩展] 跳过 Nemotron STT（未找到 nemoasr 环境，python provision.py --create --only nemoasr 可创建）
    )
) else (
    echo [跳过扩展服务] 仅核心链路运行。需要换脸/唱歌/高清口型等：set START_EXTRAS=1 后重新运行本脚本。
)

echo.
echo ============================================
echo  启动完成
echo ============================================
echo   AvatarHub:   http://127.0.0.1:9000/ui   ← 统一入口
echo   LipSync:     http://127.0.0.1:8090/health
echo   VCam:        http://127.0.0.1:7870/health
echo ============================================
echo.
echo 等待核心链路就绪（实时探针 ready.py，就绪即放行，不再盲等）...
if "%START_EXTRAS%"=="1" (
    "%FACEFUSION_PY%" "%BASE_DIR%\ready.py" --all
) else (
    "%FACEFUSION_PY%" "%BASE_DIR%\ready.py"
)

echo.
echo ========== 新引擎自检（VoxCPM2 / Nemotron3.5）==========
rem Generic /health only proves "process is up"; the slow/fragile part of these two engines is "model loaded",
rem so we also read the model_loaded/loaded flag, so the launcher sees model readiness directly.
if "%START_EXTRAS%"=="1" (
    if exist "%VOXCPM_PY%" (
        "%FACEFUSION_PY%" -c "import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:7856/health',timeout=4));print('  [OK] VoxCPM2(7856) online  model_loaded='+str(d.get('model_loaded')))" 2>nul || echo   [--] VoxCPM2(7856) not ready (first run downloads/loads the model, retry later)
    ) else (
        echo   [跳过] VoxCPM2 未安装环境（python provision.py --create --only voxcpm）
    )
    if exist "%NEMOASR_PY%" (
        "%FACEFUSION_PY%" -c "import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:7857/health',timeout=4));print('  [OK] Nemotron3.5(7857) online  loaded='+str(d.get('loaded'))+' (lazy-load: model loads on first transcription)')" 2>nul || echo   [--] Nemotron3.5(7857) not ready (model loads on first transcription)
    ) else (
        echo   [跳过] Nemotron3.5 未安装环境（python provision.py --create --only nemoasr）
    )
) else (
    echo   [跳过] 未启用扩展；set START_EXTRAS=1 后重跑可启动并自检 VoxCPM2 / Nemotron3.5
)
echo ========================================================
echo.
echo ============== 一键自检（doctor.py）==============
"%FACEFUSION_PY%" "%BASE_DIR%\doctor.py"
echo ==================================================
echo.
echo 全部 ✓ 即可访问 http://127.0.0.1:9000/ui
pause
