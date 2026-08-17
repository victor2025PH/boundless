# Lively pilot attempt 4 (runs ON the target box). ASCII-ONLY source.
# The web player cancels top-level navigation away from the wallpaper file (attempt 3:
# renderer alive, zero tcp to hub) -> embed the hub HUD in a fullscreen IFRAME instead
# (subresource load, not a navigation; hud_server sends no X-Frame-Options).
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

$proj = Join-Path $env:LOCALAPPDATA 'Lively Wallpaper\Library\wallpapers\boundless-hud'
New-Item -ItemType Directory -Force -Path $proj | Out-Null
$html = '<!doctype html><html><head><meta charset="utf-8"><title>BOUNDLESS HUD</title>' +
  '<style>html,body{margin:0;height:100%;background:#05060F;overflow:hidden}' +
  'iframe{position:fixed;inset:0;width:100vw;height:100vh;border:0}</style></head>' +
  '<body><iframe src="' + $hud + '" allow="autoplay"></iframe></body></html>'
[IO.File]::WriteAllText((Join-Path $proj 'index.html'), $html, [Text.UTF8Encoding]::new($false))
Write-Output 'IFRAME_HTML_WRITTEN'

Start-Process -FilePath $exe -ArgumentList @('setwp', '--file', ('"' + $proj + '"'))
Start-Sleep -Seconds 12
Write-Output 'SETWP4_DONE'
