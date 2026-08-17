# Set each machine's lock screen to its showcase wallpaper (no IP / no account info on
# a lock screen anyone walking by can photograph). Uses PersonalizationCSP (HKLM) which
# needs an elevated token: Windows OpenSSH gives admin users a high-integrity session,
# so EVERY box (including this one) is configured via ssh - loopback included.
# ASCII-ONLY source.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Data = Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json

$psBody = @(
  "`$img = 'C:\Users\Public\boundless-hud\wallpapers\showcase.png'"
  "if (-not (Test-Path `$img)) { Write-Output 'LOCK_SKIP no showcase'; exit 0 }"
  "`$k = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\PersonalizationCSP'"
  "New-Item -Path `$k -Force | Out-Null"
  "Set-ItemProperty -Path `$k -Name LockScreenImagePath -Value `$img"
  "Set-ItemProperty -Path `$k -Name LockScreenImageUrl -Value `$img"
  "Set-ItemProperty -Path `$k -Name LockScreenImageStatus -Value 1 -Type DWord"
  "Write-Output ('LOCK_OK ' + `$env:COMPUTERNAME)"
) -join '; '
$b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($psBody))

foreach ($m in $Data.machines) {
  $alias = [string]$m.ssh[0]
  Write-Host ("===== {0} =====" -f $m.id) -ForegroundColor Green
  ssh -o BatchMode=yes -o ConnectTimeout=12 $alias "powershell -NoProfile -EncodedCommand $b64" 2>&1 |
    Where-Object { $_ -match 'LOCK_' }
}
Write-Host 'deploy_lockscreen done' -ForegroundColor Cyan
