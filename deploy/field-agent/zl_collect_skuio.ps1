# zl_collect.ps1 -- field-agent 现场取证一键脚本（v1.2.1，2026-09-03；v1.2 在 PS5.1 下报告 0 字节：函数名 R 撞内置别名 Invoke-History、$since 撞参数 $Since）
#
# 为什么要有这个脚本：Cursor 对「读工作区外文件 / 压缩 / 向外网 POST」三类动作各弹一次
# 安全确认，用户不点允许 agent 就卡死（0903 钧机实录：Cursor 提示危险、包从未上传）。
# 把三类动作收进一个只读脚本，agent 每次只跑同一条命令；用户在 Cursor 里对这条命令
# 点一次「允许并记住」，之后全自动。脚本自身只读智聊日志、只写 C:\zhiliao-agent\snapshots。
#
# 用法（agent 调用）：
#   powershell -ExecutionPolicy Bypass -File C:\zhiliao-agent\zl_collect.ps1 -Task selfcheck
#   powershell -ExecutionPolicy Bypass -File C:\zhiliao-agent\zl_collect.ps1 -Task report -Note "发图失败" -Since 30 -Keywords "send_media,media_send,delivered=False"
#   powershell -ExecutionPolicy Bypass -File C:\zhiliao-agent\zl_collect.ps1 -Task verify -Version 1.0.71
#   powershell -ExecutionPolicy Bypass -File C:\zhiliao-agent\zl_collect.ps1 -Task reupload    # 只重传最近一份报告，不重新取证
# 输出最后一行固定为  CODE=XXXXXX  （6 位短码）或  CODE=UPLOAD_FAILED（此时 report 在 snapshots 里，用户手发群）。
#
# 上传重试（v1.3.0，#161）：0903 钧机 zl_collect 出 UPLOAD_FAILED 而 VPS 侧无任何请求
# 记录＝请求根本没出客户机（客户端网络），而当时只重试一次、隔 3 秒——家用网络抖动
# 的典型恢复窗口比这长得多。改 1+3 次、退避 5/15/30s；仍失败时报告已在 snapshots，
# 用 -Task reupload 可单独重传（不必让用户重跑一遍取证）。
#
# 隐私：不读 config/、不读任何 token/cookie；日志摘录按行截取，密钥形态（sk-/Bearer/token=）整行打码。

param(
    [ValidateSet("selfcheck", "report", "verify", "reupload")]
    [string]$Task = "report",
    [string]$Note = "",          # 一句话症状（进 report 首行与上传 meta）
    [int]   $Since = 30,         # 抓最近 N 分钟的日志行
    [string]$Keywords = "",      # 逗号分隔的过滤关键词；空=不过滤（只按时间窗）
    [string]$Version = "",       # verify 任务：期望版本号（如 1.0.71）
    [int]   $MaxLines = 500
)
$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

# ── 本机常量（装机时按机器改这一段）─────────────────────────────────────────
$Owner   = "skuio"                                   # 值守识别用：jun / skuio
$Fp      = "DC8F-0935-5EE4-F3D9"                   # 机器码
$AppRoot = Join-Path $env:APPDATA "telegram-ai-desktop"
$DataDir = Join-Path $AppRoot "data"
$LogMain = Join-Path $AppRoot "logs\backend.log"   # 主日志（data 的兄弟目录 logs）
$LogRend = Join-Path $AppRoot "logs\renderer.log"
$SideDir = Join-Path $DataDir "logs"               # wa-sidecar.log / msg-sidecar.log / fatal_traceback.log
$Work    = "C:\zhiliao-agent"
$Snap    = Join-Path $Work "snapshots"
New-Item -ItemType Directory -Force -Path $Snap | Out-Null

$ScriptVer = "1.3.0"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$windowStart = (Get-Date).AddMinutes(-$Since)
$report = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$s) { $report.Add($s) }
function Redact([string]$s) {
    if ($s -match '(?i)(sk-[A-Za-z0-9]{8,}|Bearer\s+\S+|token[=:]\s*\S+|api_key[=:]\s*\S+|password[=:]\s*\S+)') { return "[redacted line]" }
    return $s
}
function TailWindow([string]$path, [datetime]$from, [string[]]$kws, [int]$cap) {
    if (-not (Test-Path $path)) { return @("(missing: $path)") }
    $out = New-Object System.Collections.Generic.List[string]
    # 主日志行首 "[2026-09-03 13:10:42]"；边车行首 ISO "2026-09-03T05:10:42.123Z"
    $lines = Get-Content -Path $path -Tail 20000 -Encoding UTF8
    foreach ($ln in $lines) {
        $t = $null
        if ($ln -match '^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]') { $t = [datetime]::ParseExact($Matches[1], "yyyy-MM-dd HH:mm:ss", $null) }
        elseif ($ln -match '^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})') { $t = ([datetime]::Parse($Matches[1] + "Z")).ToLocalTime() }
        if ($t -and $t -lt $from) { continue }
        if ($kws.Count -gt 0) {
            $hit = $false
            foreach ($k in $kws) { if ($k -and $ln -match [regex]::Escape($k)) { $hit = $true; break } }
            if (-not $hit) { continue }
        }
        $out.Add((Redact $ln))
        if ($out.Count -ge $cap) { break }
    }
    return $out
}
function AppVersion {
    try {
        $exe = Get-Process -Name "智聊", "ChatX", "telegram-ai-desktop" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($exe) { $v = (Get-Item $exe.Path).VersionInfo.ProductVersion; if ($v) { return $v.Trim() } }
    } catch {}
    try {
        $b = Get-Content $LogMain -Tail 5000 -Encoding UTF8 | Select-String -Pattern "boot version=([\d.]+)" | Select-Object -Last 1
        if ($b) { return $b.Matches[0].Groups[1].Value }
    } catch {}
    return "unknown"
}

function Send-Report([string]$path) {
    # 上传一份报告，失败按 5/15/30s 退避重试 3 次（共 4 发）。返回短码或空串。
    $zipPath = Join-Path $Work "up.zip"
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path $path -DestinationPath $zipPath -Force
    $noteS = ("field-agent:{0}:{1}:v{3}:{2}" -f $Owner, $Task, ($Note -replace '[\r\n"]', ' '), $ScriptVer)
    $noteS = $noteS.Substring(0, [Math]::Min(190, $noteS.Length))
    $metaJson = @{ app = $ver; fp = $Fp; note = $noteS } | ConvertTo-Json -Compress
    $waits = @(0, 5, 15, 30)
    for ($i = 0; $i -lt $waits.Count; $i++) {
        if ($waits[$i] -gt 0) {
            # Write-Host 而非 Write-Output：函数返回值走管道，进度行混进去会让
            # 调用方把「upload retry…」当成短码打进最后一行 CODE=。
            Write-Host ("upload retry {0}/3 in {1}s ..." -f $i, $waits[$i])
            Start-Sleep -Seconds $waits[$i]
        }
        try {
            $r = Invoke-RestMethod -Uri "https://bd2026.cc/api/diag-upload" -Method Post -InFile $zipPath -ContentType "application/zip" -Headers @{ "x-diag-meta" = $metaJson } -TimeoutSec 60
            if ($r.ok -and $r.code) { return [string]$r.code }
        } catch {}
    }
    return ""
}

$ver = AppVersion

if ($Task -eq "reupload") {
    # 只重传最近一份报告：上次 UPLOAD_FAILED 后报告已在本机，重跑取证既慢又会
    # 换掉现场（日志窗口已经滚过去了）。
    $last = Get-ChildItem -Path $Snap -Filter "*_report.md" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $last) {
        Write-Output "CODE=NO_REPORT (snapshots 里没有报告可重传，请先跑 -Task report)"
        exit 3
    }
    Write-Output "report=$($last.FullName)"
    $code = Send-Report $last.FullName
    if ($code) { Write-Output "CODE=$code"; exit 0 }
    Write-Output "CODE=UPLOAD_FAILED (上传失败（网络），报告已保存在 $($last.FullName)，可把 .md 直接发群)"
    exit 2
}

Add-Line "# field-agent:${Owner}:${Task}  (zl_collect v$ScriptVer)"
Add-Line "- time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  fp: $Fp  app: $ver"
if ($Note) { Add-Line "- note: $Note" }
Add-Line ""

switch ($Task) {
    "selfcheck" {
        Add-Line "## paths"
        foreach ($p in @($DataDir, $LogMain, $LogRend, (Join-Path $SideDir "wa-sidecar.log"), (Join-Path $SideDir "msg-sidecar.log"), (Join-Path $SideDir "fatal_traceback.log"))) {
            $ok = Test-Path $p
            $sz = if ($ok) { (Get-Item $p).Length } else { 0 }
            $mt = if ($ok) { (Get-Item $p).LastWriteTime.ToString("MM-dd HH:mm") } else { "-" }
            Add-Line ("- {0}  exists={1}  size={2}  mtime={3}" -f $p, $ok, $sz, $mt)
        }
        Add-Line ""; Add-Line "## backend.log tail (last 5, redacted)"
        foreach ($l in (Get-Content $LogMain -Tail 5 -Encoding UTF8)) { Add-Line (Redact $l) }
    }
    "verify" {
        Add-Line "## version check"
        Add-Line ("- expected: {0}  actual: {1}  match: {2}" -f $Version, $ver, ($ver -like "$Version*"))
        Add-Line ""; Add-Line "## feature log presence (last $Since min)"
        foreach ($k in @("voice_burst", "speech_verdict", "camp", "peer_delete", "goal-inject", "degenerate", "outbound_text_guard", "sendpoint")) {
            $n = (TailWindow $LogMain $windowStart @($k) 2000).Count
            Add-Line ("- {0}: {1} lines" -f $k, $n)
        }
        Add-Line ""; Add-Line "## backend spawn lines"
        foreach ($l in (Get-Content $LogMain -Tail 20000 -Encoding UTF8 | Select-String -Pattern "backend spawn" | Select-Object -Last 3)) { Add-Line $l.Line }
    }
    "report" {
        $kws = @()
        if ($Keywords) { $kws = $Keywords.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ } }
        Add-Line "## backend.log (last $Since min, keywords=[$Keywords], cap $MaxLines)"
        foreach ($l in (TailWindow $LogMain $windowStart $kws $MaxLines)) { Add-Line $l }
        Add-Line ""; Add-Line "## sidecars (last $Since min)"
        foreach ($f in @("wa-sidecar.log", "msg-sidecar.log")) {
            Add-Line "### $f"
            foreach ($l in (TailWindow (Join-Path $SideDir $f) $windowStart @() 120)) { Add-Line $l }
        }
        Add-Line ""; Add-Line "## renderer.log (last $Since min, errors only)"
        foreach ($l in (TailWindow $LogRend $windowStart @("error", "Error", "failed", "Failed") 80)) { Add-Line $l }
        Add-Line ""; Add-Line "## fatal_traceback.log tail"
        foreach ($l in (Get-Content (Join-Path $SideDir "fatal_traceback.log") -Tail 30 -Encoding UTF8)) { Add-Line (Redact $l) }
    }
}

$mdPath = Join-Path $Snap ("{0}_{1}_report.md" -f $stamp, $Task)
[System.IO.File]::WriteAllText($mdPath, [string]::Join("`r`n", $report.ToArray()), (New-Object System.Text.UTF8Encoding($true)))
if ((Get-Item $mdPath).Length -lt 40) {
    Write-Output "report=$mdPath"
    Write-Output "CODE=EMPTY_REPORT (脚本版本 v$ScriptVer；报告为空，请把本行发给值守)"
    exit 3
}
Write-Output "report=$mdPath"
$code = Send-Report $mdPath
if ($code) { Write-Output "CODE=$code"; exit 0 }
Write-Output "CODE=UPLOAD_FAILED (上传失败（网络），报告已保存在 $mdPath，可把 .md 直接发群；网络恢复后可跑 -Task reupload 重传)"
exit 2
