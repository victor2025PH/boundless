# persona_quiz_nightly.ps1 — 夜间跑一遍人设一致性考题回归，分数不合格/下滑即告警。
# 每人设：build_quiz → 真人设 prompt 逐题问 LLM → 判分 → 落 config/persona_quiz.db，
# 与该人设历史均分比 delta；不合格或下滑走 host_alert（日志 + EventBus + 弹窗）。
#
# 建议注册（⚠ 本脚本不自动注册，运营决策；05:00 = AvatarPrerenderNightly 04:30 之后，
# 两者都在低峰但不抢同一分钟的 GPU/云端配额）：
#   schtasks /Create /TN PersonaQuizNightly /SC DAILY /ST 05:00 /F ^
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File D:\workspace\boundless\engines\chengjie\scripts\persona_quiz_nightly.ps1"
#
# 开关：config.local.yaml::personas.quiz.nightly.enabled（false 时 CLI 直接 exit 0）。
# 透传参数：本脚本的所有多余参数原样交给 CLI，如
#   .\scripts\persona_quiz_nightly.ps1 --dry-run
#   .\scripts\persona_quiz_nightly.ps1 --personas lin_jiaxin --limit 6 --no-alert
# 退出码：与 CLI 一致 —— 0 全合格 / 1 有不合格或下滑 / 2 执行异常。
# 日志：logs\quiz\quiz_<yyyyMMdd_HHmmss>.log（UTF-8，保留最近 14 份）。

param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 日志编码统一 UTF-8：PowerShell 5 的 *>> 会按 UTF-16 混写中文输出成乱码
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$logDir = Join-Path $root "logs\quiz"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $logDir "quiz_$ts.log"

"[quiz-nightly] start $ts root=$root args=$($Rest -join ' ')" | Out-File $log -Encoding utf8

if ($Rest) {
    python -m scripts.persona_quiz_nightly @Rest 2>&1 | Out-File $log -Append -Encoding utf8
} else {
    python -m scripts.persona_quiz_nightly 2>&1 | Out-File $log -Append -Encoding utf8
}
$rc = $LASTEXITCODE
"[quiz-nightly] exit=$rc" | Out-File $log -Append -Encoding utf8

# 清理 14 份以前的旧日志
Get-ChildItem $logDir -Filter "quiz_*.log" | Sort-Object Name -Descending |
    Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue

"[quiz-nightly] done" | Out-File $log -Append -Encoding utf8
exit $rc
