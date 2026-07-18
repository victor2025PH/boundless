$ErrorActionPreference = 'Continue'
$ssh = Join-Path $env:USERPROFILE '.ssh'
New-Item -ItemType Directory -Force -Path $ssh | Out-Null
foreach ($name in @('cluster_controller', 'id_ed25519')) {
  $src = Join-Path 'C:\Users\Public' $name
  $dst = Join-Path $ssh $name
  if (Test-Path $src) {
    Copy-Item $src $dst -Force
    $pubSrc = "$src.pub"
    if (Test-Path $pubSrc) { Copy-Item $pubSrc "$dst.pub" -Force }
    icacls $dst /inheritance:r | Out-Null
    Write-Output "KEYFILE_OK $name"
  }
}
