@echo off
REM ============================================================
REM  build_bestcam.bat - build BestCam MF virtual camera
REM  Uses VS Build Tools 2022 vcvars64 + bundled cmake (NMake)
REM  Output: vendor\BestCam\build\BestCamSource.dll + BestCamHost.exe
REM ============================================================
setlocal
set "VSBT=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
set "VCVARS=%VSBT%\VC\Auxiliary\Build\vcvars64.bat"
set "CMAKE=%VSBT%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"

if not exist "%VCVARS%" (echo [build] vcvars64.bat not found & exit /b 1)
if not exist "%CMAKE%" set "CMAKE=cmake"

cd /d "%~dp0vendor\BestCam" || exit /b 1

echo [build] init MSVC env...
call "%VCVARS%" >nul || exit /b 1

echo [build] cmake configure (NMake Makefiles, Release)...
"%CMAKE%" -S . -B build -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release || exit /b 1

echo [build] compiling...
"%CMAKE%" --build build || exit /b 1

echo [build] done. artifacts:
dir /b build\*.dll build\*.exe 2>nul
exit /b 0
