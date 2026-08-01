# 获客增长环周审周批（P24，2026-08-01）——每周六 07:20 计划任务 GrowthReviewWeekly。
# 跑 7 天窗口：win-rate 表 + 指令拟稿/画像填充漏斗 + 指令遵循抽检样本（人耳读），
# 并追加趋势行 logs/goals/growth_trend.jsonl（won/drive_draft/profile_fills 周走势）。
# 只读 HTTP（/api/goals/readiness + report + instr-samples），实例没起 = CLI 如实报错
# 落日志（不发消息、不写业务库，安全常跑）。
# 与 proactive_review_weekly.ps1 同模式：UTF-8 显式（PS5 *>> 混写 UTF-16 坑）。
$ErrorActionPreference = "Continue"
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $engine "logs\goals"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "growth_review_weekly.log"
# 计划任务上下文没有 AITR_WEB_TOKEN → 从实例配置自取（不把密钥写进任务定义）。
# 取不到就保持现状（CLI 默认 admin），401 会如实进日志而不是静默装绿。
if (-not $env:AITR_WEB_TOKEN) {
    foreach ($cfgPath in @(
        "D:\chengjie-instances\zhiliao\data\config\config.local.yaml",
        "D:\chengjie-instances\zhiliao\data\config\config.yaml")) {
        try {
            $raw = Get-Content $cfgPath -Raw -Encoding UTF8 -ErrorAction Stop
            $m = [regex]::Match($raw, 'auth_token:\s*[''"]?([^\s''"#]+)')
            if ($m.Success) { $env:AITR_WEB_TOKEN = $m.Groups[1].Value; break }
        } catch { }
    }
}
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== growth review weekly @ $stamp ===" | Out-File -Append -Encoding utf8 $log
python -m scripts.growth_review --days 7 --samples 12 `
    --out-jsonl "logs\goals\growth_trend.jsonl" 2>&1 |
    Out-File -Append -Encoding utf8 $log
# 日志超 5MB 轮转一份（保留一代，与轻量周批日志体量匹配）
if ((Get-Item $log -ErrorAction SilentlyContinue).Length -gt 5MB) {
    Move-Item -Force $log "$log.1"
}
