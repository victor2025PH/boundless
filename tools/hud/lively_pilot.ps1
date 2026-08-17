# Lively web-wallpaper pilot bootstrap (runs ON the target box via ssh). ASCII-ONLY source.
# P1 gray-scale: ONE machine (lianbei) gets the live HUD page as its actual desktop
# wallpaper through Lively (WebView2). Static PNG board stays underneath as fallback layer;
# sentinel warning wallpapers are hidden while Lively runs - known gap, see plan doc 16.x.
$ErrorActionPreference = 'Continue'
$cands = @(
  (Join-Path ${env:ProgramFiles(x86)} 'Lively Wallpaper\Lively.exe'),
  (Join-Path $env:ProgramFiles 'Lively Wallpaper\Lively.exe'),
  (Join-Path $env:LOCALAPPDATA 'Programs\Lively Wallpaper\Lively.exe')
)
$exe = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) { Write-Output 'LIVELY_NOT_FOUND'; exit 1 }
Write-Output ('LIVELY_EXE=' + $exe)

$hud = 'http://192.168.0.176:7913/hud?machine=' + $env:COMPUTERNAME
# machine id comes from config.json written by deploy_hud.ps1 (id field), not hostname
try {
  $cfg = Get-Content 'C:\Users\Public\boundless-hud\config.json' -Raw | ConvertFrom-Json
  if ($cfg.id) { $hud = 'http://' + [string]$cfg.hub_ip + ':7913/hud?machine=' + [string]$cfg.id }
} catch {}
Write-Output ('HUD_URL=' + $hud)

# 1) autostart task (interactive session; action via launcher FILE - quoting discipline)
$launch = 'Start-Process -FilePath "' + $exe + '"'
[IO.File]::WriteAllText('C:\Users\Public\boundless-hud\lively_launch.ps1', $launch, [Text.UTF8Encoding]::new($false))
schtasks /create /f /tn BoundlessLively /sc onlogon /tr 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\Public\boundless-hud\lively_launch.ps1' | Out-Null
schtasks /run /tn BoundlessLively | Out-Null
Start-Sleep -Seconds 14

# 2) one-shot: set the HUD page as wallpaper (command forwarded to the running instance)
$setw = 'Start-Process -FilePath "' + $exe + '" -ArgumentList @(''setwp'',''--file'',''' + $hud + ''')'
[IO.File]::WriteAllText('C:\Users\Public\boundless-hud\lively_setwp.ps1', $setw, [Text.UTF8Encoding]::new($false))
schtasks /create /f /tn BoundlessLivelySet /sc onlogon /tr 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\Public\boundless-hud\lively_setwp.ps1' | Out-Null
schtasks /run /tn BoundlessLivelySet | Out-Null
Start-Sleep -Seconds 8
schtasks /delete /f /tn BoundlessLivelySet | Out-Null
Write-Output 'LIVELY_PILOT_STARTED'
