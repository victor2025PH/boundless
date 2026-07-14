@echo off
rem ASCII-only launcher for the memory watchdog.
rem Uses %~dp0 so the (Chinese) folder path comes from the OS at runtime,
rem never hard-coded into this file -> encoding-proof.
call "%~dp0env_config.bat"
set "PYEXE=%FACEFUSION_PY%"
rem Graceful healing: above the hard threshold, call each service's /gc first; only
rem restart if that fails (avoids interrupting live work). Quoted set (no trailing
rem space) so the child process inherits the var. ASCII-only (chcp-proof).
set "MEMWD_TRY_GC=1"
start "MemWatchdog" /MIN cmd /k "chcp 65001 >nul && "%PYEXE%" "%~dp0mem_watchdog.py""
