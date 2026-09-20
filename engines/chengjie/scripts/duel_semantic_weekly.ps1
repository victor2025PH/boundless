# Duel semantic-layer eval, weekly batch -> append summary to trend JSONL.
#
# What it guards: the semantic judge (sycophancy / persona_fact / ill_timed) is
# graded against FIXED goldens, so a weekly run answers one question the nightly
# run cannot: "did the cloud model or the prompts drift?" The cloud model version
# can change under us silently (this box runs deepseek-v4-flash, a reasoning model
# whose token behaviour already bit us once).
#
# Register:
#   schtasks /Create /TN DuelSemanticWeekly /SC WEEKLY /D SAT /ST 07:10 /F ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\workspace\boundless\engines\chengjie\scripts\duel_semantic_weekly.ps1"
# Timing: 07:10 SAT keeps clear of TranslationEvalWeekly (06:30) so the two do not
# fight for the same cloud quota / CPU.
#
# ASCII-ONLY ON PURPOSE: PowerShell 5.1 reads .ps1 as GBK on this box; CJK in the
# body gets mangled and breaks the parser (same lesson as watchdog_emotion_tts.ps1
# and duel_nightly.ps1).
#
# Missing goldens or no cloud key -> run_eval exits 0 and the trend line simply
# records the corpus-shape track; a gap in the trend is itself the signal.

$ErrorActionPreference = "Continue"
$Root = Split-Path $PSScriptRoot -Parent
$Canonical = "D:\boundless\engines\chengjie"
if ((Test-Path (Join-Path $Canonical "main.py")) -and ($Root -ne $Canonical)) {
    $Root = $Canonical
}
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"

$trend = "logs/eval/duel_semantic_trend.jsonl"
$log   = "logs/eval/duel_semantic_weekly.log"
New-Item -ItemType Directory -Force -Path "logs/eval" | Out-Null
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Value "[$ts] duel semantic weekly start"

# Corpus-shape track first (zero network): if the goldens themselves are broken
# there is no point burning cloud calls.
& python -m scripts.run_eval --duel-semantic --out-jsonl $trend *>> $log
$shape = $LASTEXITCODE
Add-Content -Path $log -Value "[$ts] corpus-shape exit=$shape"

if ($shape -eq 0) {
    $env:EVAL_LLM = "1"
    & python -m scripts.run_eval --duel-semantic --out-jsonl $trend *>> $log
    $llm = $LASTEXITCODE
    Remove-Item Env:EVAL_LLM -ErrorAction SilentlyContinue
    Add-Content -Path $log -Value "[$ts] llm-track exit=$llm (informational)"
} else {
    Add-Content -Path $log -Value "[$ts] goldens broken -> skipped llm track"
}

# Exit code reflects the CORPUS-SHAPE track only, never the LLM track.
#
# Why: the semantic layer is a probabilistic detector. Measured on real-noise
# goldens, per-axis recall flakes run to run (persona_fact hit 3/3 in one run and
# 2/3 in the next with identical code). Gating a scheduled task on a coin-flip
# turns the task red at random, and a task that is red at random gets ignored.
# The signal is the TREND line (logs/eval/duel_semantic_trend.jsonl) -- read it
# with scripts/duel_trend_report.py. A broken golden corpus, by contrast, is a
# deterministic defect and DOES fail the task.
# Same philosophy as translation_eval_weekly: "trend line shows the gap, the
# script does not alarm".
exit $shape

# Keep the weekly log bounded (same policy as logs/prerender)
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 2MB)) {
    Move-Item -Force $log "$log.1"
}

exit $llm
