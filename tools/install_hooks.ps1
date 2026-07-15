# tools/install_hooks.ps1 — 把 tools/hooks/* 装进本机 .git/hooks（每台 clone 后各跑一次）。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root 'tools\hooks'
$dst = Join-Path $root '.git\hooks'
if (-not (Test-Path $dst)) { Write-Error "不是 git 仓或缺 .git/hooks: $dst"; exit 1 }
Get-ChildItem $src -File | ForEach-Object {
  Copy-Item $_.FullName (Join-Path $dst $_.Name) -Force
  Write-Output ("installed hook: {0}" -f $_.Name)
}
Write-Output "hooks 安装完成（pre-push 会在每次 push 前跑 repo_doctor 门禁）。"
