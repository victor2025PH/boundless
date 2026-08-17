# Lively pilot attempt 5 FINAL (runs ON the target box). ASCII-ONLY source.
# closewp first (setwp to the same folder may no-op on a live player), switch the
# project to Type 3 (url wallpaper) pointing straight at the hub HUD, setwp again,
# then dump forensic evidence: netstat, webview cmdlines, lively log tail.
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

Start-Process -FilePath $exe -ArgumentList @('closewp', '--monitor', '-1')
Start-Sleep -Seconds 5

$proj = Join-Path $env:LOCALAPPDATA 'Lively Wallpaper\Library\wallpapers\boundless-hud'
New-Item -ItemType Directory -Force -Path $proj | Out-Null
$info = '{"AppVersion":"2.2.1.0","Title":"BOUNDLESS HUD","Thumbnail":"","Preview":"",' +
  '"Desc":"cluster live board","Author":"BOUNDLESS","License":"","Contact":"",' +
  '"Type":3,"FileName":"' + $hud + '","Arguments":"","IsAbsolutePath":false,"Id":"boundless-hud"}'
[IO.File]::WriteAllText((Join-Path $proj 'LivelyInfo.json'), $info, [Text.UTF8Encoding]::new($false))
Write-Output 'URLTYPE_WRITTEN'

Start-Process -FilePath $exe -ArgumentList @('setwp', '--file', ('"' + $proj + '"'))
Start-Sleep -Seconds 14

Write-Output '--- netstat 7913 ---'
netstat -no | Select-String '7913' | ForEach-Object { $_.Line.Trim() }
Write-Output '--- webview cmdlines (lively-related) ---'
Get-CimInstance Win32_Process -Filter "name='msedgewebview2.exe'" |
  Where-Object { $_.CommandLine -match 'Lively|boundless|7913' } |
  Select-Object -First 3 | ForEach-Object { $_.CommandLine.Substring(0, [Math]::Min(260, $_.CommandLine.Length)) }
Write-Output '--- lively log tail ---'
$log = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Lively Wallpaper\logs') -Filter '*.txt' -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($log) { Get-Content $log.FullName -Tail 14 | ForEach-Object { $_ } } else { Write-Output 'no log' }
Write-Output 'SETWP5_DONE'
