@echo off
REM ============================================================
REM  publish_release.bat - double-click entry for the release orchestrator.
REM  Locates the BASE conda python (where conda-pack lives) and runs
REM  publish_release.py, passing through any args (--dry-run / --verify-remote
REM  [--sample N] / --hash). ASCII-only on purpose (encoding-proof).
REM
REM  Override the interpreter explicitly:  set AVATARHUB_PY=D:\miniconda3\python.exe
REM  Skip the end pause (CI):              set AVATARHUB_NOPAUSE=1
REM ============================================================
setlocal
set "ROOT=%~dp0"

REM --- locate base conda python (same detection as env_config.bat) ---
set "PY="
if defined AVATARHUB_PY if exist "%AVATARHUB_PY%" set "PY=%AVATARHUB_PY%"
if not defined PY if defined CONDA_ROOT if exist "%CONDA_ROOT%\python.exe" set "PY=%CONDA_ROOT%\python.exe"
if not defined PY if exist "%USERPROFILE%\Miniconda3\python.exe" set "PY=%USERPROFILE%\Miniconda3\python.exe"
if not defined PY if exist "%USERPROFILE%\Anaconda3\python.exe" set "PY=%USERPROFILE%\Anaconda3\python.exe"
if not defined PY if exist "C:\ProgramData\Miniconda3\python.exe" set "PY=C:\ProgramData\Miniconda3\python.exe"
if not defined PY for /f "delims=" %%I in ('where python 2^>nul') do if not defined PY set "PY=%%I"
if not defined PY for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%I"

if not defined PY (
  echo [ERROR] No base conda python found. conda-pack is required on the build machine.
  echo         Set it explicitly:  set AVATARHUB_PY=C:\path\to\miniconda3\python.exe
  goto :end
)
echo [info] python: %PY%

"%PY%" "%ROOT%publish_release.py" %*
set "RC=%ERRORLEVEL%"
echo.
echo [publish_release] exit code = %RC%

:end
if not defined AVATARHUB_NOPAUSE pause
endlocal & exit /b %RC%
