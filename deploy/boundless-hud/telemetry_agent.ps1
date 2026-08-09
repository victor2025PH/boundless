# GPU telemetry agent - second-level cluster stats feeder (HUD P3 telemetry slice,
# 2026-08-07, see plan doc section 19). ASCII-ONLY source.
# Design:
#   - ONE persistent "nvidia-smi --loop" child (no per-sample process spawns; the 60s
#     sentinel probe stays as the minute-level fallback and warning-state judge).
#   - POSTs every ~3s to the hub hud_server (:7913 /api/telemetry); errors swallowed,
#     LAN hiccups never kill the loop.
#   - Started and revived by sentinel.ps1 (v5) each minute; single-instance via pid file.
#   - -i 0 pins GPU 0 (deterministic on any future multi-GPU box).
$ErrorActionPreference = 'Continue'
$Base = 'C:\Users\Public\boundless-hud'
$PidFile = Join-Path $Base 'telemetry.pid'
try {
  $cfg = Get-Content (Join-Path $Base 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
} catch { exit 0 }
$hubIp = if ($cfg.is_hub) { '127.0.0.1' } else { [string]$cfg.hub_ip }
$url = 'http://' + $hubIp + ':7913/api/telemetry'   # hud_server PORT (kept in sync manually)
$mid = [string]$cfg.id
if (-not $mid) { exit 0 }

# single instance: exit quietly when another live agent holds the pid file
if (Test-Path $PidFile) {
  try {
    $old = [int](Get-Content $PidFile -ErrorAction Stop)
    if ($old -ne $PID -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) { exit 0 }
  } catch {}
}
Set-Content $PidFile -Value $PID -Encoding ascii

$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $smi) { exit 0 }

while ($true) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $smi.Source
  $psi.Arguments = '-i 0 --query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader,nounits -l 3'
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  try { $p = [System.Diagnostics.Process]::Start($psi) } catch { Start-Sleep -Seconds 30; continue }
  while (-not $p.HasExited) {
    $line = $p.StandardOutput.ReadLine()
    if ($null -eq $line) { break }
    if ($line -match '^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)') {
      $body = ('{"id":"' + $mid + '","vu":' + $Matches[1] + ',"vt":' + $Matches[2] +
               ',"u":' + $Matches[3] + ',"temp":' + $Matches[4] + '}')
      try {
        Invoke-RestMethod -Uri $url -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 2 | Out-Null
      } catch {}
    }
  }
  try { $p.Kill() } catch {}
  Start-Sleep -Seconds 5   # driver reset / smi exit: relaunch the loop
}
