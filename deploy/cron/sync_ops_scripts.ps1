# sync_ops_scripts.ps1 — 引擎机运维脚本路径同步（实施28）：只拉 deploy/cron，跳过 LFS smudge。
#
# 为何不用整仓 git pull：
#   1) 引擎机常有脏工作区（历史 WIP / scp 应急文件），整仓 merge 易被挡住；
#   2) 老板 LFS 媒体（showcase 视频等）在引擎机无业务价值，smudge 会拖垮/失败 pull；
#   3) 运营只需最新 cron 包装壳与哨兵——路径级 checkout 足够且安全。
#
# 用法（管理员 PowerShell，仓库根或本目录均可）：
#   powershell -ExecutionPolicy Bypass -File deploy\cron\sync_ops_scripts.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\cron\sync_ops_scripts.ps1 -Branch main

[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$Branch   = 'main',   # 跟踪的远端分支
    [switch]$NoLfsSkipConfig      # 不写本地 filter.lfs skip（默认会写，永久跳过 smudge）
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
}
if (-not (Test-Path (Join-Path $RepoRoot '.git'))) {
    throw "不是 git 仓库根：$RepoRoot"
}

Set-Location $RepoRoot
$env:GIT_LFS_SKIP_SMUDGE = '1'

if (-not $NoLfsSkipConfig) {
    # 本机 clone 永久跳过 LFS 内容下载（引擎机不需要品牌视频/字体大文件）
    git config --local filter.lfs.smudge 'git-lfs smudge --skip -- %f'
    git config --local filter.lfs.process 'git-lfs filter-process --skip'
    git config --local lfs.fetchexclude '*'
    Write-Host '[lfs] 已配置本 clone 跳过 smudge/fetch（可 -NoLfsSkipConfig 跳过此步）'
}

Write-Host "[fetch] origin/$Branch ..."
git fetch origin $Branch
$remoteSha = (git rev-parse --short "origin/$Branch").Trim()
if (-not $remoteSha) { throw "无法解析 origin/$Branch" }

# 只覆盖运维脚本目录；不触碰其它脏文件 / 不推进 HEAD
Write-Host "[checkout] origin/$Branch -- deploy/cron  (remote=$remoteSha)"
git checkout "origin/$Branch" -- deploy/cron

$sentinel = Join-Path $RepoRoot 'deploy\cron\cron_sentinel.ps1'
$len = if (Test-Path $sentinel) { (Get-Item $sentinel).Length } else { 0 }
Write-Host "[ok] deploy/cron 已与 origin/$Branch@$remoteSha 对齐；cron_sentinel.ps1 len=$len"
Write-Host '[note] HEAD 未移动；若需整仓对齐请另做（脏树场景勿盲目 reset --hard）'
exit 0
