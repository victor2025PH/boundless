# ChatX 1.0.5 internal-test remote upgrade + observation probe (ASCII-only on purpose:
# PS5.1 reads BOM-less UTF-8 as ANSI/GBK; any CJK literal here would mojibake on Win10).
# Steps: verify installer hash -> kill running app -> silent uninstall -> silent install
#        -> registry/files verify -> real first-boot probe with installed backend (seeds
#        user data area exactly like the desktop shell would) -> report seeded items.
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$expected = 'D8C327058FBDDCB12D40D504874A28624AB65A8489209B90927A9B94196D179E'
$desk  = [Environment]::GetFolderPath('Desktop')
$setup = Join-Path $desk 'ChatX-Setup-1.0.5.exe'
"HOST|$env:COMPUTERNAME|$env:USERNAME"
"FREEGB|" + [math]::Round((Get-PSDrive C).Free/1GB, 1)
if (!(Test-Path $setup)) { "FATAL|installer-missing|$setup"; exit 2 }
$h = (Get-FileHash $setup -Algorithm SHA256).Hash
"HASH|$(($h -eq $expected))"
if ($h -ne $expected) { "FATAL|hash-mismatch|$h"; exit 3 }

$app = Join-Path $env:LOCALAPPDATA 'Programs\telegram-ai-desktop'

# 1) stop anything running out of the install dir (app shell, backend sidecar, node sidecars)
$procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    try { $_.Path -and $_.Path -like "$app*" } catch { $false } })
"RUNNING|" + (($procs | ForEach-Object { $_.ProcessName }) -join ',')
$procs | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

# 2) silent uninstall of the old version (synchronous via _?= so -Wait is real)
$un = Get-ChildItem $app -Filter 'Uninstall*.exe' -File -ErrorAction SilentlyContinue | Select-Object -First 1
$regBefore = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.UninstallString -like '*telegram-ai-desktop*' }
"OLDVER|" + (($regBefore | ForEach-Object { $_.DisplayVersion }) -join ',')
if ($un) {
    $t0 = Get-Date
    Start-Process -FilePath $un.FullName -ArgumentList '/currentuser','/S',"_?=$app" -Wait
    "UNINSTALL|ok|" + [math]::Round(((Get-Date)-$t0).TotalSeconds,1) + "s"
} else {
    "UNINSTALL|no-uninstaller-found"
}
Start-Sleep -Seconds 2
"OLDDIR-REMAIN|" + (Test-Path $app)

# 3) silent install of 1.0.5
$t1 = Get-Date
$p = Start-Process -FilePath $setup -ArgumentList '/S','/currentuser' -Wait -PassThru
"INSTALL|exit=$($p.ExitCode)|" + [math]::Round(((Get-Date)-$t1).TotalSeconds,1) + "s"

# 4) verify registry + payload on disk
$reg = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.UninstallString -like '*telegram-ai-desktop*' }
"REGVER|" + (($reg | ForEach-Object { $_.DisplayVersion }) -join ',')
$rootExe = Get-ChildItem $app -Filter '*.exe' -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike 'Uninstall*' } | Select-Object -First 1
"APPEXE|" + $(if ($rootExe) { $rootExe.Name } else { 'MISSING' })
"BACKEND|" + (Test-Path (Join-Path $app 'resources\backend\backend.exe'))
"SEEDMANIFEST|" + (Test-Path (Join-Path $app 'resources\seed-data\seed-manifest.json'))
$sd = Join-Path $app 'resources\seed-data'
if (Test-Path $sd) {
    "SEEDFILES|" + (Get-ChildItem $sd -Recurse -File | Measure-Object).Count
}
"WABAILEYS|" + (Test-Path (Join-Path $app 'resources\services\whatsapp-baileys\server.js'))
"MSGRWEB|"   + (Test-Path (Join-Path $app 'resources\services\messenger-web\server.js'))

# 5) real first-boot probe: run the installed backend against the real user data dir,
#    exactly like the desktop shell (AITR_DATA_DIR=%APPDATA%\telegram-ai-desktop\data).
#    This performs/verifies seeding now, so testers open an already-seeded app.
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
    # surface backend seeding log lines (CJK pattern built from codepoints to keep file ASCII)
    $pat = [string]([char]0x64AD) + [char]0x79CD   # bo-zhong "seeding"
    if (Test-Path $log) {
        Select-String -Path $log -Pattern $pat -ErrorAction SilentlyContinue |
            Select-Object -First 14 | ForEach-Object { "LOG|" + $_.Line.Trim() }
    }
} else {
    "FIRSTBOOT|skipped-no-backend"
}
"DONE|1.0.5"
