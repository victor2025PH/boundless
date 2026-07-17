# Probe SSH to all peers (skip self). Prefer explicit controller key to avoid agent hang.
$ErrorActionPreference = 'Continue'
$me = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -First 1).IPAddress
$map = @(
  @{ n='huansheng'; ip='192.168.0.176'; u='user'; k='id_ed25519' },
  @{ n='tongyi'; ip='192.168.0.117'; u='Administrator'; k='cluster_controller' },
  @{ n='zhituo'; ip='192.168.0.198'; u='Administrator'; k='cluster_controller' },
  @{ n='huanyan-node'; ip='192.168.0.104'; u='Administrator'; k='cluster_controller' },
  @{ n='tongchuan-node'; ip='192.168.0.140'; u='Administrator'; k='cluster_controller' }
)
$keyDir = Join-Path $env:USERPROFILE '.ssh'
foreach ($p in $map) {
  if ($p.ip -eq $me) { Write-Output ("SKIP self " + $p.n); continue }
  $key = Join-Path $keyDir $p.k
  if (-not (Test-Path $key)) { $key = Join-Path $keyDir 'cluster_controller' }
  $o = ssh -o BatchMode=yes -o ConnectTimeout=5 -o IdentitiesOnly=yes -i $key "$($p.u)@$($p.ip)" "hostname" 2>&1
  if ($LASTEXITCODE -eq 0) { Write-Output ("OK " + $p.n + " -> " + $o) }
  else { Write-Output ("FAIL " + $p.n + " -> " + $o) }
}
