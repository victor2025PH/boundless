@echo off
rem ==========================================
rem  Desktop launcher - one-click control console.
rem  Prefers the branded PySide6 GUI (.venv_launcher); falls back to the
rem  zero-dependency tkinter GUI (facefusion env) if PySide6 is unavailable.
rem  ASCII-only on purpose (encoding-proof).
rem ==========================================
call "%~dp0env_config.bat"

set "QT_PY=%~dp0.venv_launcher\Scripts\pythonw.exe"
if exist "%QT_PY%" (
    start "AvatarHub Launcher" "%QT_PY%" "%~dp0launcher_qt.py"
    goto :eof
)

rem -- Fallback: tkinter GUI on the facefusion env python --
set "GUI_PY=%FACEFUSION_PY%"
if not exist "%GUI_PY%" set "GUI_PY=python"
start "AvatarHub Launcher" "%GUI_PY%" "%~dp0launcher.py"
