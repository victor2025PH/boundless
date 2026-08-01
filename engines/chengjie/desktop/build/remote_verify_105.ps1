# Verify-only + first-boot probe for ChatX 1.0.5 (no uninstall/install). ASCII-only.
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$app = Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'
"HOST|$env:COMPUTERNAME|$env:USERNAME"
$reg = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.UninstallString -like '*telegram-ai-desktop*' }
"REGVER|" + (($reg | ForEach-Object { $_.DisplayVersion }) -join ',')
$rootExe = Get-ChildItem $app -Filter '*.exe' -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
"APPEXE|" + $(if ($rootExe) { $rootExe.Name } else { 'MISSING' })
"BACKEND|" + (Test-Path (Join-Path $app 'resources\backend\backend.exe'))
"SEEDMANIFEST|" + (Test-Path (Join-Path $app 'resources\seed-data\seed-manifest.json'))
$sd = Join-Path $app 'resources\seed-data'
if (Test-Path $sd) { "SEEDFILES|" + (Get-ChildItem $sd -Recurse -File | Measure-Object).Count }
"WABAILEYS|" + (Test-Path (Join-Path $app 'resources\services\whatsapp-baileys\server.js'))
"MSGRWEB|"   + (Test-Path (Join-Path $app 'resources\services\messenger-web\server.js'))

$be = Join-Path $app 'resources\backend\backend.exe'
$dd = Join-Path $env:APPDATA 'telegram-ai-desktop\data'
"DATADIR-PRE|" + (Test-Path (Join-Path $dd 'config\config.yaml'))
if (Test-Path $be) {
    New-Item -ItemType Directory -Force -Path (Join-Path $dd 'config') | Out-Null
    $port = Get-Random -Minimum 42000 -Maximum 59000
    $log  = Join-Path $env:TEMP 'chatx-probe-105.log'
    Remove-Item $log,"$log.err" -ErrorAction SilentlyContinue
    $env:AITR_DESKTOP_MODE = '1'
    $env:AITR_WEB_HOST = '127.0.0.1'
    $env:AITR_WEB_PORT = "$port"
    $env:AITR_WEB_TOKEN = 'probe-token'
    $env:AITR_DATA_DIR = $dd
    $env:AITR_CONFIG_PATH = Join-Path $dd 'config\config.yaml'
    $env:AITR_APP_VERSION = '1.005'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    $env:HOST_ALERT_SILENT = '1'
    $bp = Start-Process -FilePath $be -WorkingDirectory $dd -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    $t2 = Get-Date; $ready = $false
    while (((Get-Date) - $t2).TotalSeconds -lt 150) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/desktop/ping" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch {}
        if ($bp.HasExited) { break }
        Start-Sleep -Milliseconds 1500
    }
    "FIRSTBOOT|ready=$ready|" + [math]::Round(((Get-Date)-$t2).TotalSeconds,1) + "s|exited=$($bp.HasExited)"
    try {
        $login = Invoke-WebRequest -Uri "http://127.0.0.1:$port/login" -UseBasicParsing -TimeoutSec 5
        "LOGINPAGE|$($login.StatusCode)"
    } catch { "LOGINPAGE|fail" }
    foreach ($item in 'config\config.local.yaml','config\profiles_runtime.yaml','config\voice_refs',
                      'config\persona_albums','config\prerender_lines','assets\voices',
                      'config\knowledge_base.db','config\persona_bio.db','config\persona_media.db') {
        "SEEDED|$item|" + (Test-Path (Join-Path $dd $item))
    }
    if (!$bp.HasExited) { Stop-Process -Id $bp.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    $pat = [string]([char]0x64AD) + [char]0x79CD
    if (Test-Path $log) {
        Select-String -Path $log -Pattern $pat -ErrorAction SilentlyContinue |
            Select-Object -First 14 | ForEach-Object { "LOG|" + $_.Line.Trim() }
    }
} else {
    "FIRSTBOOT|skipped-no-backend"
}
"VERIFY-DONE|1.0.5"
