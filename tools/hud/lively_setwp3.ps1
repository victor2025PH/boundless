# Lively pilot attempt 3 (runs ON the target box). ASCII-ONLY source.
# CLI setwp auto-imports MEDIA files only; .html must already exist as a library
# project -> create a proper wallpaper project (LivelyInfo.json Type=1 web) in the
# library folder, then setwp --file <project folder>.
$ErrorActionPreference = 'Continue'
$exe = @(
  (Join-Path ${env:ProgramFiles(x86)} 'Lively Wallpaper\Lively.exe'),
  (Join-Path $env:ProgramFiles 'Lively Wallpaper\Lively.exe'),
  (Join-Path $env:LOCALAPPDATA 'Programs\Lively Wallpaper\Lively.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) { Write-Output 'LIVELY_NOT_FOUND'; exit 1 }

$hud = 'http://192.168.0.176:7913/hud'
try {
  $cfg = Get-Content 'C:\Users\Public\boundless-hud\config.json' -Raw | ConvertFrom-Json
  if ($cfg.id) { $hud = 'http://' + [string]$cfg.hub_ip + ':7913/hud?machine=' + [string]$cfg.id }
} catch {}

$settings = Join-Path $env:LOCALAPPDATA 'Lively Wallpaper\Settings.json'
Write-Output ('FIRSTRUN_DONE=' + (Test-Path $settings))

$proj = Join-Path $env:LOCALAPPDATA 'Lively Wallpaper\Library\wallpapers\boundless-hud'
New-Item -ItemType Directory -Force -Path $proj | Out-Null

$html = '<!doctype html><html><head><meta charset="utf-8"><title>BOUNDLESS HUD</title>' +
  '<style>body{background:#05060F}</style></head><body>' +
  '<script>location.replace("' + $hud + '");</script></body></html>'
[IO.File]::WriteAllText((Join-Path $proj 'index.html'), $html, [Text.UTF8Encoding]::new($false))

$info = '{"AppVersion":"2.2.1.0","Title":"BOUNDLESS HUD","Thumbnail":"","Preview":"",' +
  '"Desc":"cluster live board","Author":"BOUNDLESS","License":"","Contact":"",' +
  '"Type":1,"FileName":"index.html","Arguments":"","IsAbsolutePath":false,"Id":"boundless-hud"}'
[IO.File]::WriteAllText((Join-Path $proj 'LivelyInfo.json'), $info, [Text.UTF8Encoding]::new($false))
Write-Output ('PROJECT=' + $proj)

Start-Process -FilePath $exe -ArgumentList @('setwp', '--file', ('"' + $proj + '"'))
Start-Sleep -Seconds 12
$wv = @(Get-Process -Name msedgewebview2 -ErrorAction SilentlyContinue).Count
Write-Output ('WEBVIEW_PROCS=' + $wv)
Write-Output 'SETWP3_DONE'
