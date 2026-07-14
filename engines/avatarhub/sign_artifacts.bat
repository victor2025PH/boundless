@echo off
REM ============================================================
REM  sign_artifacts.bat - Authenticode-sign AvatarHub artifacts.
REM  Signs dist\AvatarHub.exe and dist\AvatarHub-Setup-*.exe so
REM  Windows SmartScreen / AV trust the download (no scary warning).
REM  ASCII-only on purpose (encoding-proof).
REM
REM  Configure ONE signing source via environment variable:
REM    set AVATARHUB_SIGN_PFX=C:\path\cert.pfx  ^& set AVATARHUB_SIGN_PFX_PW=pw   (OV file)
REM    set AVATARHUB_SIGN_SHA1=<thumbprint>                                       (cert in store / EV token)
REM    set AVATARHUB_SIGN_SUBJECT="Your Company Co.,Ltd"                          (cert in store by subject)
REM  Optional timestamp server (default DigiCert RFC3161):
REM    set AVATARHUB_SIGN_TS=http://timestamp.digicert.com
REM
REM  NOTE: EV code signing (hardware token / cloud HSM) gives instant SmartScreen
REM        reputation; OV builds reputation over time. Buy from DigiCert/GlobalSign/etc.
REM ============================================================
setlocal
set "ROOT=%~dp0"
if "%AVATARHUB_SIGN_TS%"=="" set "AVATARHUB_SIGN_TS=http://timestamp.digicert.com"

REM --- locate signtool.exe (PATH, then Windows SDK) ---
set "SIGNTOOL="
for /f "delims=" %%I in ('where signtool 2^>nul') do if not defined SIGNTOOL set "SIGNTOOL=%%I"
if not defined SIGNTOOL for /f "delims=" %%I in ('dir /b /s "%ProgramFiles(x86)%\Windows Kits\10\bin\*\x64\signtool.exe" 2^>nul') do if not defined SIGNTOOL set "SIGNTOOL=%%I"
if not defined SIGNTOOL (
  echo [ERROR] signtool.exe not found. Install the Windows 10/11 SDK.
  exit /b 1
)
echo [info] signtool: %SIGNTOOL%

REM --- require a cert source ---
if not defined AVATARHUB_SIGN_PFX if not defined AVATARHUB_SIGN_SHA1 if not defined AVATARHUB_SIGN_SUBJECT (
  echo [ERROR] No signing cert configured. Set one of:
  echo   AVATARHUB_SIGN_PFX ^(+ AVATARHUB_SIGN_PFX_PW^)  /  AVATARHUB_SIGN_SHA1  /  AVATARHUB_SIGN_SUBJECT
  exit /b 1
)

set "RC=0"
call :sign "%ROOT%dist\AvatarHub.exe"
for %%F in ("%ROOT%dist\AvatarHub-Setup-*.exe") do call :sign "%%~fF"
echo.
if "%RC%"=="0" (echo [done] all present artifacts signed and verified.) else (echo [warn] some signing/verify steps failed.)
exit /b %RC%

:sign
set "F=%~1"
if not exist "%F%" (echo [skip] not found: %F% & goto :eof)
echo [sign] %F%
if defined AVATARHUB_SIGN_PFX (
  "%SIGNTOOL%" sign /fd SHA256 /tr "%AVATARHUB_SIGN_TS%" /td SHA256 /f "%AVATARHUB_SIGN_PFX%" /p "%AVATARHUB_SIGN_PFX_PW%" "%F%"
) else if defined AVATARHUB_SIGN_SHA1 (
  "%SIGNTOOL%" sign /fd SHA256 /tr "%AVATARHUB_SIGN_TS%" /td SHA256 /sha1 %AVATARHUB_SIGN_SHA1% "%F%"
) else (
  "%SIGNTOOL%" sign /fd SHA256 /tr "%AVATARHUB_SIGN_TS%" /td SHA256 /n %AVATARHUB_SIGN_SUBJECT% "%F%"
)
if errorlevel 1 (set "RC=1" & echo [ERROR] sign failed: %F% & goto :eof)
"%SIGNTOOL%" verify /pa "%F%"
if errorlevel 1 set "RC=1"
goto :eof
