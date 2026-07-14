@echo off
rem ============================================================
rem boot_stack.bat - one-shot boot autostart for the call/live stack.
rem Registered as Scheduled Task "AvatarStack_Boot" (ONLOGON, 20s delay).
rem Starts: watchdog + hub + fish + interpreter + nemo + lipsync + vcam.
rem Each service is skipped if its port is already LISTENING (idempotent:
rem safe to re-run manually; no duplicate processes / port-bind crashes).
rem Remote services (STT .140 etc.) are probed by the watchdog, not here.
rem ASCII-only on purpose (encoding-proof under any codepage).
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0env_config.bat"
set "OMP_WAIT_POLICY=PASSIVE"
set "KMP_BLOCKTIME=0"

call :ensure 9000 "hub"         "%~dp0_launch_hub_detached.bat"
call :ensure 7855 "fish"        "%~dp0_launch_fish_local.bat"
call :ensure 7900 "interpreter" "%~dp0_launch_interp.bat"
rem P5d 2026-07-10: LinXiaoling SBV2 JP-Extra emotion TTS (ja interp route).
call :ensure 7861 "sbv2_tts"    "%~dp0_launch_sbv2_local.bat"
rem nemo_stt moved to .140 on 2026-07-05 (SVC_NEMO_STT in env_config points there;
rem NemoSTT_Boot task on .140 self-starts it). To run locally again: uncomment the
rem next line AND comment the SVC_NEMO_STT/INTERP_NEMO_WS lines in env_config.bat.
rem call :ensure 7857 "nemo_stt"    "%~dp0_launch_nemo_local.bat"
call :ensure 8090 "lipsync"     "%~dp0_launch_lipsync_local.bat"

call :ensure 7870 "vcam"        "%~dp0_launch_vcam_local.bat"

rem watchdog last (it revives hub/fish/interpreter if any of the above failed).
rem wmic is removed on Win11 26xxx -> detect via PowerShell CIM (exit 0 = already running).
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name like '%%python%%'\" | Where-Object { $_.CommandLine -match 'mem_watchdog\.py' }) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo [boot] starting mem_watchdog
    call "%~dp0start_mem_watchdog.bat"
) else (
    echo [boot] mem_watchdog already running, skip
)
exit /b 0

:ensure
rem %1=port %2=name %3=launcher
netstat -ano | findstr /R /C:":%~1 .*LISTENING" >nul 2>&1
if errorlevel 1 (
    echo [boot] starting %~2
    start "%~2" /MIN cmd /c "%~3"
) else (
    echo [boot] %~2 already listening on %~1, skip
)
exit /b 0
