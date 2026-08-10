# AI duel bench - nightly batch (runner -> auto judge -> nonzero exit if over budget)
#
# Purpose: turn "run duels by hand + read transcripts by hand" into an unattended
# nightly job. Scenario cards carry `expect_defects` caps; once a defect class is
# fixed its cap goes to 0, so any regression turns the job red.
#
# ASCII-ONLY ON PURPOSE: PowerShell 5.1 reads .ps1 as GBK on this box, so CJK in
# the script body gets mangled and breaks the parser (same lesson as
# scripts/watchdog_emotion_tts.ps1). Keep all strings ASCII here.
#
# Timing discipline (all learned the hard way):
#   - Avoid 04:30 AvatarPrerenderNightly (same GPU) -> schedule around 02:10.
#   - --pace between turns: 2026-07-28 measured that unpaced hammering drags the
#     health probe into the "fake alive" window; the watchdog restarted the
#     instance twice inside 30 min (FLAP threshold).
#   - Keep turns-per-scenario small: defect density comes from scenario variety,
#     not from long single runs.
#
# Scheduled use:
#   powershell -ExecutionPolicy Bypass -File scripts\duel_nightly.ps1
# Manual smoke:
#   ... -Turns 3 -Scenarios "price_haggler,mem_poison"

param(
    [int]    $Turns     = 8,
    [double] $Pace      = 2.0,
    [string] $Scenarios = "",            # empty = all scenario cards
    [string] $Base      = "http://127.0.0.1:18799",
    [int]    $KeyBase   = 990001000,     # reserved test range; never real chats
    [switch] $SkipHealth
)

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $PSScriptRoot
# Production process loads D:\boundless\engines\chengjie\main.py. The scheduled
# task historically pointed at the D:\workspace\... hardlink twin; without this
# pin, night logs land in a different tree than GET /api/admin/duel-bench reads.
$Canonical = "D:\boundless\engines\chengjie"
if ((Test-Path (Join-Path $Canonical "main.py")) -and ($Root -ne $Canonical)) {
    $Root = $Canonical
}
$ScenDir = Join-Path $Root "config\duel_scenarios"
$Stamp   = Get-Date -Format "yyyyMMdd_HHmm"
$OutDir  = Join-Path $Root "logs\duel\nightly_$Stamp"
$LogFile = Join-Path $Root "logs\duel\nightly_$Stamp.log"
$env:PYTHONIOENCODING = "utf-8"
# Ensure `python -m scripts.*` resolves against the same tree we write logs to.
Set-Location $Root

function Say([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

# PS 5.1's `*>> file` writes UTF-16LE while Add-Content -Encoding utf8 writes UTF-8.
# Mixing both in one file makes the log unreadable - measured on the 2026-07-29
# 02:10 run, whose whole transcript came out as mojibake (same lesson already
# learned on avatar_prerender_nightly.ps1; this script had not been fixed).
# Route every child-process stream through Out-File -Encoding utf8 instead.
function LogRun([scriptblock]$block) {
    & $block 2>&1 | Out-File -FilePath $LogFile -Append -Encoding utf8
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Say "duel nightly start out=$OutDir turns=$Turns pace=$Pace"

if (-not $env:AITR_WEB_TOKEN) {
    $cfg = "D:\chengjie-instances\zhiliao\data\config\config.local.yaml"
    if (Test-Path $cfg) {
        $m = Select-String -Path $cfg -Pattern '^\s*auth_token:\s*(\S+)' | Select-Object -First 1
        if ($m) { $env:AITR_WEB_TOKEN = $m.Matches[0].Groups[1].Value }
    }
}
if (-not $env:AITR_WEB_TOKEN) { Say "no AITR_WEB_TOKEN, abort"; exit 2 }

# Do not run against an unhealthy instance: engine failures would be recorded
# as a pile of bogus defects.
if (-not $SkipHealth) {
    try {
        $code = (Invoke-WebRequest -Uri "$Base/login" -TimeoutSec 15 -UseBasicParsing).StatusCode
        if ($code -ne 200) { Say "instance /login=$code, skip tonight"; exit 0 }
    } catch { Say "instance unreachable, skip tonight"; exit 0 }
}

$cards = Get-ChildItem $ScenDir -Filter *.json | Sort-Object Name
if ($Scenarios) {
    $want = $Scenarios.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    $cards = $cards | Where-Object { $want -contains $_.BaseName }
}
if (-not $cards) { Say "no matching scenario cards, abort"; exit 2 }

$i = 0
foreach ($c in $cards) {
    $i++
    $key = $KeyBase + $i
    Say ("run {0} chat_key={1}" -f $c.BaseName, $key)
    try {
        LogRun { python -m scripts.duel_runner --scenario $c.FullName `
            --chat-key $key --turns $Turns --pace $Pace `
            --base $Base --out-dir $OutDir }
    } catch {
        Say ("scenario {0} failed" -f $c.BaseName)
    }
    Start-Sleep -Seconds 5   # breathe between scenarios too
}

Say "judging"
$judge   = Join-Path $OutDir "transcript_*.jsonl"
$Summary = Join-Path $Root "logs\duel\latest_summary.json"
$Trend   = Join-Path $Root "logs\duel\duel_trend.jsonl"
$LastRun = Join-Path $Root "logs\duel\LAST_RUN.json"
# --semantic: three axes all have goldens now (see config/eval/duel_semantic_samples.yaml),
# so the semantic findings are trustworthy enough to gate on.
# --alert-on-over: host_alert on over-budget (log + desktop toast; webhook separate).
LogRun { python -m scripts.duel_judge --glob $judge --strict --semantic `
    --summary-out $Summary --trend-out $Trend --alert-on-over }
$rc = $LASTEXITCODE
if (Test-Path $Summary) { Say "summary -> $Summary" }
if (Test-Path $Trend)   { Say "trend   -> $Trend" }
if (Test-Path $LastRun) { Say "last_run -> $LastRun" }
Say ("judge done exit={0} (0 = every scenario within expect_defects)" -f $rc)

# Close open drill-range cases left by tonight's process_message chain.
# Without this, media_complaint / ai_doubt lines from duel cards stay on /cases
# until a human clears them (2026-08-09 production residue). Best-effort: a
# failed cleanup must NOT flip the judge exit code.
try {
    $hdr = @{ Authorization = "Bearer $($env:AITR_WEB_TOKEN)" }
    $body = '{"resolution":"drill nightly cleanup"}'
    $resp = Invoke-RestMethod -Method POST -Uri "$Base/api/cases/close-drill" `
        -Headers $hdr -ContentType "application/json; charset=utf-8" `
        -Body $body -TimeoutSec 30
    $n = 0
    if ($resp -and ($null -ne $resp.closed)) { $n = [int]$resp.closed }
    Say ("drill cases closed={0}" -f $n)
} catch {
    Say ("drill case cleanup skipped: {0}" -f $_.Exception.Message)
}

# Keep 14 logs, same policy as logs/prerender
Get-ChildItem (Join-Path $Root "logs\duel") -Filter "nightly_*.log" |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip 14 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $rc
