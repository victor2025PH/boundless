@echo off
title OpenClaw Worker Upgrade
echo ============================================
echo   OpenClaw Worker One-Click Upgrade
echo ============================================
echo.

cd /d C:\openclaw\mobile-auto-project

REM Find Python
set PYTHON=
for %%P in (
    "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
    "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
) do (
    if exist %%~P (
        set PYTHON=%%~P
        goto :found
    )
)
where python >nul 2>&1 && set PYTHON=python
:found

if "%PYTHON%"=="" (
    echo [ERROR] Python not found!
    pause
    exit /b 1
)

echo Python: %PYTHON%
echo.
echo [1/4] Pulling update from Coordinator...
%PYTHON% -c "import urllib.request,json; r=urllib.request.urlopen('http://192.168.0.118:8000/cluster/update-package/info',timeout=5); print('  Coordinator OK:', json.loads(r.read()))" 2>nul || echo   Trying ZeroTier...
echo.

echo [2/4] Stopping current service...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000.*LISTENING"') do (
    taskkill /F /PID %%a 2>nul
)
timeout /t 2 /nobreak >nul

echo [3/4] Downloading and applying update...
%PYTHON% -c "
import urllib.request, zipfile, io, shutil, os, sys

urls = ['http://192.168.0.118:8000/cluster/update-package',
        'http://10.222.142.172:8000/cluster/update-package']

data = None
for url in urls:
    try:
        print(f'  Trying {url}...')
        resp = urllib.request.urlopen(url, timeout=30)
        data = resp.read()
        print(f'  Downloaded {len(data)//1024} KB')
        break
    except Exception as e:
        print(f'  Failed: {e}')

if not data:
    print('  [ERROR] Cannot reach Coordinator!')
    sys.exit(1)

# Extract (skip config/ to preserve local settings)
proj = r'C:\openclaw\mobile-auto-project'
with zipfile.ZipFile(io.BytesIO(data)) as zf:
    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if name.startswith('mobile-auto-project/'):
            rel = name[len('mobile-auto-project/'):]
        else:
            rel = name
        if rel.startswith('config/'):
            continue
        target = os.path.join(proj, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, 'wb') as dst:
            dst.write(src.read())

print('  Update applied!')
"
echo.

echo [4/4] Starting service via wrapper (auto-restart enabled)...
start "OpenClaw Worker" %PYTHON% service_wrapper.py

timeout /t 15 /nobreak >nul
%PYTHON% -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5); print('  Worker is UP!')" 2>nul || echo   Starting... please wait.

echo.
echo ============================================
echo   Upgrade complete!
echo   Dashboard: http://127.0.0.1:8000/dashboard
echo   Auto-update: enabled (checks every 5min)
echo ============================================
pause
