@echo off
rem ==========================================
rem  Compile the Windows installer from AvatarHub.iss using Inno Setup (ISCC).
rem  Output: dist\AvatarHub-Setup-{AppVersion}.exe (version defined in AvatarHub.iss)
rem  Requires: dist\AvatarHub.exe built first (run build_launcher.bat), and
rem            Inno Setup 6 installed (ISCC.exe).
rem  ASCII-only on purpose (encoding-proof).
rem ==========================================
setlocal
set "HERE=%~dp0"

if not exist "%HERE%..\dist\AvatarHub.exe" (
    echo [ERROR] dist\AvatarHub.exe not found. Run build_launcher.bat first.
    exit /b 1
)

set "ISCC="
for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
) do if exist %%P set "ISCC=%%~P"

if not defined ISCC (
    where ISCC.exe >nul 2>&1 && for /f "delims=" %%I in ('where ISCC.exe') do set "ISCC=%%I"
)

if not defined ISCC (
    echo [ERROR] Inno Setup ^(ISCC.exe^) not found. Install from https://jrsoftware.org/isdl.php
    exit /b 1
)

echo [build] using %ISCC%
"%ISCC%" "%HERE%AvatarHub.iss"
echo.
echo [done] output: see ISCC line above (dist\AvatarHub-Setup-*.exe)
endlocal
