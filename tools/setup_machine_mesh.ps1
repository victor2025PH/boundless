<#
  setup_machine_mesh.ps1 — 五机网状 SSH + 开发目录 + 壁纸
  用法（在通译机 117）:
    powershell -File tools\setup_machine_mesh.ps1
    powershell -File tools\setup_machine_mesh.ps1 -SkipClone
#>
[CmdletBinding()]
param(
  [switch]$SkipClone,
  [switch]$SkipWallpaper,
  [switch]$LocalOnly
)
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Machines = (Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json).machines
$SshDir = Join-Path $env:USERPROFILE '.ssh'
$LocalIp = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -First 1).IPAddress
$Public = 'C:\Users\Public'

function Get-PubKeysToDistribute {
  $pubs = @()
  foreach ($name in @('cluster_controller.pub','id_ed25519.pub','gpuctrl_ed25519.pub','id_rsa.pub')) {
    $p = Join-Path $SshDir $name
    if (Test-Path $p) { $pubs += (Get-Content $p -Raw).Trim() }
  }
  return @($pubs | Where-Object { $_ } | Select-Object -Unique)
}

function Invoke-RemoteFile([string]$Alias, [string]$LocalScript, [string]$RemoteArgs) {
  $remote = "$Public/_boundless_tool.ps1"
  scp -o BatchMode=yes -o ConnectTimeout=12 $LocalScript "${Alias}:$($remote -replace '\\','/')" 2>&1 | Out-Null
  ssh -o BatchMode=yes -o ConnectTimeout=120 $Alias "powershell -NoProfile -ExecutionPolicy Bypass -File $remote $RemoteArgs" 2>&1
}

# 1) SSH config
Write-Host '=== render + merge local ssh config ===' -ForegroundColor Cyan
& (Join-Path $PSScriptRoot 'render_ssh_config.ps1') | Out-Null
$cfgLocal = Join-Path $Root 'deploy\ssh_config.boundless'
$marker = '# === boundless cluster BEGIN ==='
$end = '# === boundless cluster END ==='
$dstLocal = Join-Path $SshDir 'config'
$body = Get-Content $cfgLocal -Raw -Encoding UTF8
$block = "$marker`r`n$body`r`n$end`r`n"
if (Test-Path $dstLocal) {
  $cur = Get-Content $dstLocal -Raw -Encoding UTF8
  if ($cur -match [regex]::Escape($marker)) {
    $cur = [regex]::Replace($cur, '(?s)' + [regex]::Escape($marker) + '.*?' + [regex]::Escape($end), $block.TrimEnd())
    Set-Content -Path $dstLocal -Value $cur -Encoding utf8
  } else {
    Add-Content -Path $dstLocal -Value "`r`n$block" -Encoding utf8
  }
} else {
  Set-Content -Path $dstLocal -Value $block -Encoding utf8
}

# 2) local dirs
Write-Host '=== local workdirs ===' -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path 'D:\开发' | Out-Null
function Ensure-J([string]$link, [string]$target) {
  if (Test-Path $link) {
    $item = Get-Item $link -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return }
    Remove-Item $link -Recurse -Force -ErrorAction SilentlyContinue
  }
  cmd /c mklink /J "$link" "$target" | Out-Null
}
if (-not (Test-Path 'D:\boundless\.git') -and (Test-Path 'D:\workspace\boundless\.git')) {
  if (-not (Test-Path 'D:\boundless')) { cmd /c mklink /J "D:\boundless" "D:\workspace\boundless" | Out-Null }
}
Ensure-J 'D:\开发\通译' 'D:\workspace\boundless\engines\chengjie'
Ensure-J 'D:\开发\智聊' 'D:\workspace\boundless\engines\chengjie'
@(
  '本机中文名：通译',
  '同机产品：通译 + 智聊',
  '工作仓：D:\workspace\boundless',
  '开工 pull；收工 push。'
) | Set-Content 'D:\开发\README.txt' -Encoding UTF8

# 3) local wallpaper
if (-not $SkipWallpaper) {
  $wp = Join-Path $Root 'brand-assets\05_backgrounds\machines\tongyi-wallpaper.png'
  if (Test-Path $wp) {
    New-Item -ItemType Directory -Force -Path "$Public\Pictures" | Out-Null
    $dest = "$Public\Pictures\boundless-wallpaper-tongyi.png"
    Copy-Item $wp $dest -Force
    & (Join-Path $PSScriptRoot 'remote_set_wallpaper.ps1') -WallpaperPath $dest
    Write-Host 'local wallpaper set'
  }
}

if ($LocalOnly) { Write-Host 'LocalOnly done'; return }

# 4) pubs file
$pubs = Get-PubKeysToDistribute
$pubsFile = Join-Path $env:TEMP 'boundless_pubs.txt'
$pubs | Set-Content $pubsFile -Encoding ascii
Write-Host ("pubs: {0}" -f $pubs.Count)

foreach ($m in $Machines) {
  $alias = [string]$m.ssh[0]
  if ($m.ip -eq $LocalIp) { Write-Host "skip self $alias"; continue }
  Write-Host ""
  Write-Host ("===== {0} ({1} {2}) =====" -f $alias, $m.zh, $m.ip) -ForegroundColor Green

  Write-Host '-- keys --'
  scp -o BatchMode=yes $pubsFile "${alias}:C:/Users/Public/boundless_pubs.txt" 2>&1 | Out-Null
  Invoke-RemoteFile $alias (Join-Path $PSScriptRoot 'remote_install_keys.ps1') '-PubsFile C:\Users\Public\boundless_pubs.txt' | Select-Object -Last 6

  Write-Host '-- private keys (cluster_controller + id_ed25519) --'
  foreach ($pair in @(
    @{ priv='cluster_controller'; pub='cluster_controller.pub' },
    @{ priv='id_ed25519'; pub='id_ed25519.pub' }
  )) {
    $ck = Join-Path $SshDir $pair.priv
    $cp = Join-Path $SshDir $pair.pub
    if (-not (Test-Path $ck)) { continue }
    scp -o BatchMode=yes $ck ("{0}:C:/Users/Public/{1}" -f $alias, $pair.priv) 2>&1 | Out-Null
    scp -o BatchMode=yes $cp ("{0}:C:/Users/Public/{1}" -f $alias, $pair.pub) 2>&1 | Out-Null
  }
  Invoke-RemoteFile $alias (Join-Path $PSScriptRoot 'remote_install_controller_key.ps1') '' | Select-Object -Last 5

  Write-Host '-- ssh config --'
  scp -o BatchMode=yes $cfgLocal "${alias}:C:/Users/Public/boundless_ssh_config" 2>&1 | Out-Null
  Invoke-RemoteFile $alias (Join-Path $PSScriptRoot 'remote_merge_ssh_config.ps1') '-SrcConfig C:\Users\Public\boundless_ssh_config' | Select-Object -Last 4

  Write-Host '-- workdirs --'
  $prod = ([string]::Join(' / ', @($m.products)))
  $cloneFlag = if ($SkipClone) { '' } else { '-AllowClone' }
  $args = "-ZhName '$($m.zh)' -Role '$($m.role)' -Products '$prod' -SshAlias '$alias' $cloneFlag"
  Invoke-RemoteFile $alias (Join-Path $PSScriptRoot 'remote_ensure_workdir.ps1') $args | Select-Object -Last 8

  if (-not $SkipWallpaper) {
    Write-Host '-- wallpaper --'
    $rel = ([string]$m.wallpaper) -replace '/', '\'
    $localWp = Join-Path $Root $rel
    $remoteWp = "$Public\Pictures\boundless-wallpaper-$($m.id).png"
    scp -o BatchMode=yes $localWp ("{0}:C:/Users/Public/Pictures/boundless-wallpaper-{1}.png" -f $alias, $m.id) 2>&1 | Out-Null
    Invoke-RemoteFile $alias (Join-Path $PSScriptRoot 'remote_set_wallpaper.ps1') "-WallpaperPath '$remoteWp'" | Select-Object -Last 4
  }
}

Write-Host ''
Write-Host '=== mesh setup finished; run tools\cluster_ping.ps1 ===' -ForegroundColor Cyan
