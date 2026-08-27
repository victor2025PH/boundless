# song_factory_nightly.ps1 — 夜间唱歌供给巡检（实施58 P2）
# 计划任务：SongFactoryNightly（每日 05:10，低峰跑 176 GPU）。
# 干三件事（全部幂等）：
#   1) song_factory 全模板×全声库补缺/换锚重渲（新鲜度三键自动判，齐货=零 GPU）；
#   2) song_custom_worker 消化夜里积压的专属歌订单（≤5 张，GPU 饱和自动让路）；
#   3) 只读盘点写日志（备货矩阵 + 订单分状态计数）。
# 日志：logs/song_factory/nightly_<ts>.log（保留最近 14 份）。

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 日志编码统一 UTF-8（PS5 *>> 会按 UTF-16 混写中文成乱码）
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 直连口服务令牌（本机即 AvatarHub 节点的运维约定；CLI 自带同一回落）
if (-not $env:SONG_TOKEN) {
    try { $env:SONG_TOKEN = (Get-Content "D:\faceX\mfys\secrets\service_token.txt" -Raw -ErrorAction Stop).Trim() } catch {}
}

$logDir = Join-Path $root "logs\song_factory"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $logDir "nightly_$ts.log"

"[song-nightly] start $ts root=$root" | Out-File $log -Encoding utf8

# 0) 点唱台「补货申请」队列（页面一键排队 → 夜批 --force 重渲 → 清队）
$reqFile = python -c "import sys; sys.path.insert(0,'.'); from scripts._data_root import resolve_data_roots; from src.companion.song_stock import templates_dir; print(templates_dir(root=resolve_data_roots()[0]) / '_supply_requests.json')" 2>$null
$forceIds = @()
if ($reqFile -and (Test-Path $reqFile.Trim())) {
    try {
        $reqs = Get-Content $reqFile.Trim() -Raw -Encoding UTF8 | ConvertFrom-Json
        $forceIds = @($reqs | ForEach-Object { $_.template_id } | Where-Object { $_ })
    } catch {}
}
"[song-nightly] supply requests: $($forceIds -join ',')" | Out-File $log -Append -Encoding utf8

# 1) 曲库补缺（逐 enabled 模板；新鲜度三键自判，齐货=秒退零 GPU；
#    在补货申请队列里的模板带 --force 重渲）
$tpls = python -c "import sys; sys.path.insert(0,'.'); from scripts._data_root import resolve_data_roots; from src.companion.song_stock import load_song_manifest, templates_dir; print('\n'.join(t.id for t in load_song_manifest(templates_dir(root=resolve_data_roots()[0])) if t.enabled))" 2>$null
$rcFactory = 0
foreach ($tid in ($tpls -split "`n" | Where-Object { $_ -and $_.Trim() })) {
    $tidT = $tid.Trim()
    "[song-nightly] factory template=$tidT" | Out-File $log -Append -Encoding utf8
    if ($forceIds -contains $tidT) {
        python -m scripts.song_factory --template $tidT --force 2>&1 | Out-File $log -Append -Encoding utf8
    } else {
        python -m scripts.song_factory --template $tidT 2>&1 | Out-File $log -Append -Encoding utf8
    }
    if ($LASTEXITCODE -ne 0) { $rcFactory = $LASTEXITCODE }
}
"[song-nightly] factory exit=$rcFactory" | Out-File $log -Append -Encoding utf8
# 申请队列清账（本轮已消费；失败模板下轮页面可再排）
if ($reqFile -and (Test-Path $reqFile.Trim())) {
    Set-Content -Path $reqFile.Trim() -Value "[]" -Encoding UTF8
}

# 2) 专属歌订单（≤5 张；GPU 饱和 exit 3=让路不算错）
python -m scripts.song_custom_worker --max-orders 5 2>&1 | Out-File $log -Append -Encoding utf8
$rcWorker = $LASTEXITCODE
"[song-nightly] worker exit=$rcWorker" | Out-File $log -Append -Encoding utf8

# 3) 只读盘点
python -m scripts.song_factory --status 2>&1 | Out-File $log -Append -Encoding utf8

# 清理 14 份以前的旧日志
Get-ChildItem $logDir -Filter "nightly_*.log" | Sort-Object Name -Descending |
    Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue

"[song-nightly] done factory=$rcFactory worker=$rcWorker" | Out-File $log -Append -Encoding utf8
if ($rcFactory -ne 0 -and $rcFactory -ne $null) { exit $rcFactory }
if ($rcWorker -eq 3) { exit 0 }   # GPU 让路=正常夜况
exit $rcWorker
