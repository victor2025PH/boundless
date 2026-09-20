# album_restock_nightly.ps1 — 夜间处理相册场景缺口补货计划（P2 图文一致性）
# 计划任务：AlbumRestockNightly（每日 05:00，低峰跑 176 ComfyUI 出图，不与白天在线出图抢卡）。
# 计划来源：watchdog _check_media_restock 写 config/album_restock_plan.json
#（「点名场景反复要不到 + 相册零备货」自动入队；开关 companion.selfie.consistency.auto_restock）。
# 幂等：无 pending 项即退出（exit 0）；逐项落盘，中途崩溃已完成项不重跑。
# 日志：logs/restock/nightly_<ts>.log（保留最近 14 份）。

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 日志编码统一 UTF-8：PowerShell 5 的 *>> 会按 UTF-16 混写中文输出成乱码
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$logDir = Join-Path $root "logs\restock"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $logDir "nightly_$ts.log"

"[restock-nightly] start $ts root=$root" | Out-File $log -Encoding utf8

python -m scripts.album_restock 2>&1 | Out-File $log -Append -Encoding utf8
$rc = $LASTEXITCODE
"[restock-nightly] album_restock exit=$rc" | Out-File $log -Append -Encoding utf8

# 清理 14 份以前的旧日志
Get-ChildItem $logDir -Filter "nightly_*.log" | Sort-Object Name -Descending |
    Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue

"[restock-nightly] done" | Out-File $log -Append -Encoding utf8
exit $rc
