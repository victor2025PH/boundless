<#
.SYNOPSIS
  把智控后端源码 tgkz2026/backend 同步到另一台机（credpool_stage.py 的仓库外依赖）。

  tgkz2026/ 被仓库根 .gitignore 整体忽略，git clone / pull 不带它；中央凭据池（CredPoolService）
  却要 import 它的 config / admin.* / api.*。换机、重装、迁移生产实例时必须跑一次本脚本，
  否则池首起就是 ModuleNotFoundError: config（实施102 阶段 4 迁 173 实锤）。

  只同步源码：排除 data/（智控自己的生产库与会话）、node_modules、__pycache__、.pytest_cache。
  目标路径与本机相同（默认 D:\workspace\boundless\tgkz2026\backend），走 ssh 别名 + tar/scp。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\instances\sync_tgkz_backend.ps1 -To yuyan
  powershell -ExecutionPolicy Bypass -File deploy\instances\sync_tgkz_backend.ps1 -To yuyan -DryRun
#>
param(
    [Parameter(Mandatory = $true)][string]$To,
    [string]$Source = "D:\workspace\boundless\tgkz2026\backend",
    [string]$Dest = "",
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
if (-not $Dest) { $Dest = $Source }
if (-not (Test-Path (Join-Path $Source 'config.py'))) { throw "源目录不是智控后端（缺 config.py）：$Source" }

$parent = Split-Path $Source -Parent
$leaf   = Split-Path $Source -Leaf
$tar    = Join-Path $env:TEMP ("tgkz_backend_{0}.tar" -f (Get-Date -Format yyyyMMdd_HHmmss))
$excl   = @('--exclude', "$leaf/data", '--exclude', "$leaf/node_modules", '--exclude', '__pycache__', '--exclude', '.pytest_cache')

Write-Host "[sync] 打包 $Source（排除 data/node_modules/缓存）"
& tar -cf $tar -C $parent @excl $leaf
if ($LASTEXITCODE) { throw "tar 失败 rc=$LASTEXITCODE" }
if ($DryRun) { $n = (& tar -tf $tar | Measure-Object -Line).Lines; Remove-Item $tar; "[dry-run] $n 个条目会同步到 ${To}:$Dest"; exit 0 }
"[sync] 包 {0:N1} MB" -f ((Get-Item $tar).Length / 1MB)

$destParent = Split-Path $Dest -Parent
$remoteTar  = "D:\tgkz_backend_sync.tar"
& scp -o BatchMode=yes -o ConnectTimeout=8 $tar "${To}:$remoteTar"
if ($LASTEXITCODE) { throw "scp 失败 rc=$LASTEXITCODE" }
$remote = "New-Item -ItemType Directory -Force '$destParent' | Out-Null; tar -xf $remoteTar -C '$destParent'; Remove-Item $remoteTar; if (Test-Path '$Dest\config.py') { 'ok' } else { 'MISSING config.py' }"
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($remote))
$out = & ssh -o BatchMode=yes $To "powershell -NoProfile -EncodedCommand $enc"
Remove-Item $tar -ErrorAction SilentlyContinue
Write-Host "[sync] ${To}:$Dest -> $out"
if ("$out" -notmatch '^ok') { exit 1 }
