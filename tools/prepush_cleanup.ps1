# boundless/tools/prepush_cleanup.ps1 — 首次 push 到新 remote 前，一次性历史瘦身。
# 为什么放到 push 前：本仓当前只在本地、未 push；LFS/历史清理的磁盘收益只有在
# 「offload 到 remote + 本地 prune」后才真正兑现，故不在开发中途重写历史，改为 push 前一次做净。
# 前置：git-lfs 已装（117 = 3.7.1）。可选：git-filter-repo（用于彻底抹除已 untrack 的历史大对象）。
# 危险级：★重写历史★。执行前确认无人已 clone 本仓；执行后需强制 push 到全新空 remote。
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Set-Location (Split-Path -Parent $PSScriptRoot)   # 仓库根（与文件夹名无关）

Write-Output '== 1) LFS 接管字体/媒体（.gitattributes 已入库），迁移历史 =='
git lfs install --local
git lfs migrate import --everything --include="brand-assets/fonts/*.otf,brand-assets/fonts/*.ttf,*.mp4,*.mov,*.psd,*.sketch"

Write-Output '== 2)（可选）彻底抹除已 untrack 的历史大对象（需 git-filter-repo）=='
$hasFR = (& git filter-repo --version 2>$null)
if ($hasFR) {
  git filter-repo --force --invert-paths `
    --path engines/chengjie/website `
    --path engines/avatarhub/demo_record/.pwprofile
  Write-Output '   filter-repo: purged dup-site + browser cache from all history'
} else {
  Write-Output '   git-filter-repo 未安装：跳过历史抹除（HEAD 已干净；如需彻底回收，pip install git-filter-repo 后重跑）'
}

Write-Output '== 3) 回收 =='
git reflog expire --expire=now --all
git gc --prune=now --aggressive
$mb = [math]::Round((Get-ChildItem '.git' -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum/1MB,1)
Write-Output ('   .git now = ' + $mb + ' MB')
Write-Output 'PREPUSH_CLEANUP_DONE  (接着： git remote add origin <url> ; git push -u origin main)'
