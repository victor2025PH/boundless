@echo off
rem ==========================================
rem  Build the desktop launcher into a standalone Windows exe (PyInstaller).
rem  Output: dist\AvatarHub.exe  (one-file, windowed, branded icon + version info).
rem  Requires .venv_launcher with pyinstaller + pyside6-essentials (see README).
rem  ASCII-only on purpose (encoding-proof).
rem ==========================================
setlocal
set "ROOT=%~dp0"
set "VPY=%ROOT%.venv_launcher\Scripts\python.exe"
if not exist "%VPY%" (
    echo [ERROR] .venv_launcher not found. Run:
    echo   ^<facefusion python^> -m venv .venv_launcher
    echo   .venv_launcher\Scripts\python -m pip install pyside6-essentials pyinstaller pillow cryptography zstandard
    exit /b 1
)

rem Conda pythons keep OpenSSL/lzma/ffi DLLs in <env>\Library\bin (not next to python.exe),
rem so PyInstaller cannot resolve them unless that dir is on PATH at build time. Without it
rem the frozen exe has _ssl.pyd but no libssl -> https dies with URLError on clean machines
rem (works on dev boxes only because their PATH leaks conda). Derive base env from pyvenv.cfg.
set "BASEPREFIX="
for /f "tokens=1,* delims== " %%a in ('findstr /b /c:"home" "%ROOT%.venv_launcher\pyvenv.cfg"') do set "BASEPREFIX=%%b"
if exist "%BASEPREFIX%\Library\bin" (
    set "PATH=%BASEPREFIX%\Library\bin;%PATH%"
    echo [info] added to PATH for DLL resolution: %BASEPREFIX%\Library\bin
) else (
    echo [WARN] conda Library\bin not found - frozen exe may lack OpenSSL DLLs!
)

rem [2026-07-16 P8 release] pywebview native window shell shipped in the frozen exe:
rem   AvatarHub.exe --webview-shell <url> subprocess gives app-owned windows (taskbar icon =
rem   app.ico, guaranteed maximize) instead of Edge --app (ignores geometry flags when Edge
rem   already runs). collect-all pulls WebView2/pythonnet runtime DLLs; measured size delta ~+6MB.
rem   webview_shell imports pywebview via importlib on purpose - hidden-imports below are the
rem   single explicit decision point for shipping it.
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name AvatarHub ^
  --icon "%ROOT%assets\app.ico" ^
  --version-file "%ROOT%assets\version_info.txt" ^
  --collect-submodules app_config ^
  --collect-all zstandard ^
  --collect-all webview ^
  --collect-all clr_loader ^
  --collect-all pythonnet ^
  --collect-all proxy_tools ^
  --collect-all bottle ^
  --hidden-import launcher_theme ^
  --hidden-import service_manager ^
  --hidden-import app_config ^
  --hidden-import pack_installer ^
  --hidden-import zstandard ^
  --hidden-import license ^
  --hidden-import telemetry ^
  --hidden-import telemetry_client ^
  --hidden-import release_sign ^
  --hidden-import diag_pack ^
  --hidden-import cryptography ^
  --hidden-import webview_shell ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import clr ^
  "%ROOT%launcher_qt.py"
if errorlevel 1 (
    rem Fail LOUDLY. 2026-07-13: a running dist\AvatarHub.exe made the final os.remove fail,
    rem yet this script printed [done] and a stale 1.0.8 exe shipped inside the 1.0.9 installer.
    echo [FAIL] PyInstaller exited with error %errorlevel% - dist\AvatarHub.exe NOT updated!
    exit /b 1
)

echo.
echo [done] output: dist\AvatarHub.exe
endlocal
