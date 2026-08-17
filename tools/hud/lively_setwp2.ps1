# Lively pilot attempt 2 (runs ON the target box). ASCII-ONLY source.
# setwp --file only accepts local files/projects, not http URLs -> write a local
# redirect html that immediately navigates the web player to the hub HUD page.
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

$html = '<!doctype html><html><head><meta charset="utf-8"><title>BOUNDLESS HUD</title>' +
  '<style>body{background:#05060F}</style></head><body>' +
  '<script>location.replace("' + $hud + '");</script></body></html>'
$wp = 'C:\Users\Public\boundless-hud\hud_wallpaper.html'
[IO.File]::WriteAllText($wp, $html, [Text.UTF8Encoding]::new($false))
Write-Output ('WROTE ' + $wp)

Start-Process -FilePath $exe -ArgumentList @('setwp', '--file', $wp)
Start-Sleep -Seconds 10
Write-Output 'SETWP_SENT'
