# watchdog_emotion_tts.ps1 - synthesis-level watchdog for the local EmotionTTS (CosyVoice3, :7852)
#
# WHY: 2026-07-14 incident - from 13:42 to 16:01 the 7852 service was "half dead": /health kept
#   returning 200 and register_spk worked, but every real /v1/tts/clone request timed out. The
#   in-app health probe (health-only) stayed green, so ALL outbound voice silently fell back to
#   edge_tts generic voice for ~2h20m. A health probe cannot catch this failure mode; only a real
#   synthesis request can.
#
# WHAT: every run (scheduled task EmotionTTSWatchdog, 5 min):
#   1. /health unreachable            -> trigger EmotionTTS_Boot (idempotent boot bat handles it)
#   2. /health ok but models loading  -> wait; if loading persists > LoadingGraceMin, treat as jam
#   3. /health ok + loaded            -> POST a tiny real clone request (neutral, ~2 chars)
#        success -> reset strike counter, log latency, exit
#        failure/timeout -> strike++ ; strikes >= StrikeLimit -> kill 7852 process (+ leftovers by
#        command line) -> trigger EmotionTTS_Boot -> poll /health until up (or give up, next run
#        retries). Restart cooldown prevents flap loops.
#
# State:  logs\watchdog_emotion_tts.state.json   {strikes, last_restart_epoch, loading_since_epoch}
# Log:    logs\watchdog_emotion_tts.log          (self-rotating)
#
# Manual runs:
#   probe only : powershell -ExecutionPolicy Bypass -File scripts\watchdog_emotion_tts.ps1 -DryRun
#   normal     : powershell -ExecutionPolicy Bypass -File scripts\watchdog_emotion_tts.ps1
#
# NOTE: ASCII-only on purpose (PowerShell 5.1 decodes BOM-less UTF-8 as GBK and corrupts CJK
#   literals). The Chinese probe text is built from [char] codepoints below.

param(
    [string]$BaseUrl         = "http://127.0.0.1:7852",
    [string]$RefWav          = "D:\workspace\telegram-mtproto-ai\config\voice_refs\lin_xiaoyu.wav",
    [int]   $SynthTimeoutSec = 120,
    [int]   $StrikeLimit     = 2,
    [int]   $RestartCooldownMin = 30,
    [int]   $LoadingGraceMin = 15,
    [int]   $BootWaitSec     = 300,
    [string]$BootTask        = "EmotionTTS_Boot",
    [switch]$ForceRestart,
    [switch]$DryRun,
    [string]$LogPath         = "$PSScriptRoot\..\logs\watchdog_emotion_tts.log",
    [string]$StatePath       = "$PSScriptRoot\..\logs\watchdog_emotion_tts.state.json"
)

$ErrorActionPreference = "SilentlyContinue"

function Write-Log([string]$level, [string]$msg) {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
    try {
        $c = @(Get-Content $LogPath)
        if ($c.Count -gt 3000) { Set-Content -Path $LogPath -Value ($c[-2000..-1]) }
    } catch {}
}

function Read-State {
    try {
        $s = Get-Content $StatePath -Raw | ConvertFrom-Json
        return @{ strikes = [int]$s.strikes; last_restart_epoch = [double]$s.last_restart_epoch;
                  loading_since_epoch = [double]$s.loading_since_epoch }
    } catch {
        return @{ strikes = 0; last_restart_epoch = 0.0; loading_since_epoch = 0.0 }
    }
}

function Save-State($st) {
    try { ($st | ConvertTo-Json -Compress) | Set-Content -Path $StatePath } catch {}
}

function Get-Epoch { return [double]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) }

function Get-Health {
    # returns: "down" | "loading" | "ok"
    try {
        $r = Invoke-WebRequest -Uri "$BaseUrl/health" -TimeoutSec 5 -UseBasicParsing
        if ($r.StatusCode -ne 200) { return "down" }
        $j = $r.Content | ConvertFrom-Json
        if ($j.ok -and $j.models_loaded) { return "ok" }
        return "loading"
    } catch { return "down" }
}

function Invoke-SynthProbe {
    # Real clone synthesis with the production reference voice. Returns @{ok; ms; err}.
    if (-not (Test-Path $RefWav)) {
        return @{ ok = $false; skip = $true; err = "ref wav missing: $RefWav" }
    }
    $refB64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($RefWav))
    $refTxt = ""
    $sidecar = [IO.Path]::ChangeExtension($RefWav, ".txt")
    if (Test-Path $sidecar) {
        try { $refTxt = (Get-Content $sidecar -Encoding UTF8 -Raw).Trim() } catch {}
    }
    # probe text: "zai ne." ( CJK built from codepoints; keep this file ASCII )
    $text = [string]([char]0x5728) + [string]([char]0x5462) + [string]([char]0x3002)
    $payload = @{
        text                = $text
        reference_audio_b64 = $refB64
        reference_text      = $refTxt
        emotion             = "neutral"
        speed               = 1.0
        return_base64       = $true
    } | ConvertTo-Json -Compress
    $t0 = Get-Date
    try {
        $resp = Invoke-RestMethod -Uri "$BaseUrl/v1/tts/clone" -Method Post `
            -Body ([Text.Encoding]::UTF8.GetBytes($payload)) `
            -ContentType "application/json" -TimeoutSec $SynthTimeoutSec
        $ms = [int]((Get-Date) - $t0).TotalMilliseconds
        $b64 = [string]$resp.audio_base64
        if ($b64.Length -gt 1000) { return @{ ok = $true; ms = $ms } }
        return @{ ok = $false; ms = $ms; err = "empty/short audio_base64 (len=$($b64.Length))" }
    } catch {
        $ms = [int]((Get-Date) - $t0).TotalMilliseconds
        return @{ ok = $false; ms = $ms; err = "$($_.Exception.Message)" }
    }
}

function Restart-EmotionTts {
    # kill port owner + leftovers by command line, then bring back via the boot scheduled task
    $pids = @(Get-NetTCPConnection -LocalPort 7852 -State Listen |
              Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($p in $pids) {
        if ($p -gt 0) { Write-Log "KILL" "stop pid=$p (port 7852 owner)"; Stop-Process -Id $p -Force }
    }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match "emotion_tts_server" } |
        ForEach-Object { Write-Log "KILL" "stop leftover pid=$($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 3
    schtasks /Run /TN $BootTask | Out-Null
    Write-Log "BOOT" "triggered scheduled task $BootTask, waiting up to ${BootWaitSec}s"
    $deadline = (Get-Date).AddSeconds($BootWaitSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        if ((Get-Health) -eq "ok") { return $true }
    }
    return $false
}

# ── main ─────────────────────────────────────────────────────────────────────
$st = Read-State
$now = Get-Epoch

if ($ForceRestart) {
    # manual drill / apply new boot bat (e.g. to activate file logging)
    Write-Log "FORCE" "manual restart requested"
    $ok = Restart-EmotionTts
    $st.strikes = 0; $st.last_restart_epoch = $now; $st.loading_since_epoch = 0.0
    Save-State $st
    if ($ok) { Write-Log "RECOVERED" "service healthy after forced restart"; exit 0 }
    Write-Log "FAILED" "service not healthy after forced restart"
    exit 3
}

$health = Get-Health

if ($health -eq "down") {
    # cold-down / crashed: the boot bat is health-idempotent and reaps jammed zombies itself
    Write-Log "DOWN" "/health unreachable -> trigger $BootTask"
    if (-not $DryRun) { schtasks /Run /TN $BootTask | Out-Null }
    $st.loading_since_epoch = 0.0
    Save-State $st
    exit 1
}

if ($health -eq "loading") {
    if ($st.loading_since_epoch -le 0) {
        $st.loading_since_epoch = $now
        Save-State $st
        Write-Log "LOADING" "models loading, give it time"
        exit 0
    }
    $loadMin = [int](($now - $st.loading_since_epoch) / 60)
    if ($loadMin -lt $LoadingGraceMin) {
        Write-Log "LOADING" "still loading (${loadMin}m < ${LoadingGraceMin}m grace)"
        exit 0
    }
    Write-Log "JAM" "stuck in loading for ${loadMin}m -> restart"
    if (-not $DryRun) {
        $ok = Restart-EmotionTts
        $st.strikes = 0; $st.last_restart_epoch = $now; $st.loading_since_epoch = 0.0
        Save-State $st
        if ($ok) { Write-Log "RECOVERED" "service healthy after loading-jam restart" ; exit 0 }
        Write-Log "FAILED" "service still not healthy after restart (next run retries)"
        exit 3
    }
    exit 0
}

# health ok + models loaded -> real synthesis probe
$st.loading_since_epoch = 0.0
$probe = Invoke-SynthProbe

if ($probe.skip) {
    Write-Log "SKIP" "synth probe skipped: $($probe.err)"
    Save-State $st
    exit 0
}

if ($probe.ok) {
    if ($st.strikes -gt 0) { Write-Log "OK" "synth recovered (latency=$($probe.ms)ms), strikes reset" }
    else { Write-Log "OK" "synth ok latency=$($probe.ms)ms" }
    $st.strikes = 0
    Save-State $st
    exit 0
}

$st.strikes = [int]$st.strikes + 1
Write-Log "STRIKE" "synth probe failed ($($st.strikes)/$StrikeLimit) after $($probe.ms)ms: $($probe.err)"

if ($st.strikes -lt $StrikeLimit) {
    Save-State $st
    exit 2
}

# strike limit reached: half-dead (health green, synthesis dead) -> restart, with cooldown
$sinceRestartMin = if ($st.last_restart_epoch -gt 0) { ($now - $st.last_restart_epoch) / 60 } else { 1e9 }
if ($sinceRestartMin -lt $RestartCooldownMin) {
    Write-Log "COOLDOWN" ("half-dead confirmed but last restart was {0:N0}m ago (< ${RestartCooldownMin}m), holding" -f $sinceRestartMin)
    Save-State $st
    exit 2
}

if ($DryRun) {
    Write-Log "DRYRUN" "would restart EmotionTTS now (half-dead: health ok, synth failing)"
    Save-State $st
    exit 2
}

Write-Log "RESTART" "half-dead confirmed (health ok, $($st.strikes) consecutive synth failures) -> kill + reboot"
$ok = Restart-EmotionTts
$st.strikes = 0
$st.last_restart_epoch = $now
Save-State $st
if ($ok) {
    Write-Log "RECOVERED" "service healthy after half-dead restart"
    exit 0
}
Write-Log "FAILED" "service still not healthy after restart (next run retries)"
exit 3
