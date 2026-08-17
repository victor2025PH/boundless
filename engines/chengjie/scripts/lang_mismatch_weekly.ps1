# 出站语言一致性周批（P3-198，2026-08-04）——建议计划任务 LangMismatchWeekly。
# 从 inbox.db 地面真相扫 7 天窗口（含按日分桶），趋势行追加
# logs/eval/lang_mismatch_trend.jsonl —— 「语言一致率」KPI 时间线：
# 生成侧修复 + 三条投递闸上线后，mismatch_rows 应持续为 0；任何非零＝新的
# 未覆盖路径自己浮出水面（8/03 事故与 Yasmin 6 条中文晨安都是这样被抓的）。
#
# 刻意用「结果扫描」而非「闸门计数持久化」当 KPI 源：扫描量的是客户实际收到
# 什么（outcome），闸门计数量的是干预了多少（activity）；且 HOLD 事件本就落
# 投递失败审计，无需再建趋势库。与 translation_eval_weekly.ps1 同模式：
# UTF-8 显式（PS5 *>> 混写 UTF-16 坑）。
#
# 注册（未自动建，需人工决定）：
#   schtasks /Create /TN LangMismatchWeekly /SC WEEKLY /D SAT /ST 07:20 /F ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\boundless\engines\chengjie\scripts\lang_mismatch_weekly.ps1"
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\eval"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "lang_mismatch_weekly.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== lang mismatch weekly @ $stamp ===" | Out-File -Append -Encoding utf8 $log
python tools\scan_outbound_lang_mismatch.py --days 7 --top 10 `
    --out-jsonl "logs\eval\lang_mismatch_trend.jsonl" 2>&1 |
    Out-File -Append -Encoding utf8 $log
# 日志超 5MB 轮转一份（保留一代，与轻量周批日志体量匹配）
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 5MB) {
    Move-Item -Force $log "$log.1"
}
