# release_watchdog.ps1 - monitors the public download/auto-update chain for ChatX (bd2026.cc).
#
# WHY: the vision-tunnel watchdog covers the GPU path, but the download/update chain itself had no
#   monitoring. Silent-failure modes that leave installed clients unable to download or auto-update,
#   with nobody knowing:
#     - latest.yml points at an exe that is missing / truncated (e.g. pointer updated but the exe
#       upload failed) -> auto-update 404s or hash-fails
#     - manifest.json (download page) and latest.yml (updater) drift to different versions
#     - VPS disk full / nginx / pm2 down -> 5xx on the whole chain
#   These are pure alert conditions (the watchdog cannot repair a corrupt release; a human must
#   re-publish), so unlike the tunnel watchdog this one has NO repair action.
#
# It ALSO meta-monitors the tunnel watchdog: if vision_tunnel_watchdog.state.json stops updating,
# that scheduled task has likely stopped -> we would otherwise be blind to the monitor itself dying.
#
# Checks (all self-consistent, no external "expected version" needed):
#   1. latest.yml reachable + version/exe/size parseable
#   2. manifest.json version == latest.yml version (updater vs download-page must agree)
#   3. HEAD the exe latest.yml points at: HTTP 200 AND Content-Length == latest.yml size
#   4. tunnel watchdog state fresh (< StaleMin) + both scheduled tasks present
#
# Alerts reuse the telegram channel from notify_webhooks.json (single source of truth), sent
# DIRECTLY (self-contained). alerted flag prevents repeat spam; recovery notice on clear.
#
# NOTE: ASCII-only (PS5.1 GBK trap). The one Chinese label is built from codepoints.

param(
    [string]$SiteUrl        = "https://bd2026.cc",
    [int]   $StaleMin       = 20,
    [int]   $StrikeLimit    = 2,
    [string]$TunnelState    = "D:\chengjie-instances\.ops\vision_tunnel_watchdog.state.json",
    [switch]$DryRun,
    [string]$OpsDir         = "D:\chengjie-instances\.ops",
    [string]$NotifyWebhooksJson = "D:\chengjie-instances\zhiliao\data\config\notify_webhooks.json"
)

$ErrorActionPreference = "SilentlyContinue"
$LogPath   = Join-Path $OpsDir "release_watchdog.log"
$StatePath = Join-Path $OpsDir "release_watchdog.state.json"
$AlertPath = Join-Path $OpsDir "release_watchdog.alert.log"
New-Item -ItemType Directory -Force -Path $OpsDir | Out-Null

function Write-Log([string]$level, [string]$msg) {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
    try { $c = @(Get-Content $LogPath); if ($c.Count -gt 3000) { Set-Content -Path $LogPath -Value ($c[-2000..-1]) } } catch {}
}

function Read-State {
    try {
        $s = Get-Content $StatePath -Raw | ConvertFrom-Json
        return @{ strikes = [int]$s.strikes; alerted = [bool]$s.alerted }
    } catch { return @{ strikes = 0; alerted = $false } }
}
function Save-State($st) { try { ($st | ConvertTo-Json -Compress) | Set-Content -Path $StatePath } catch {} }

function Send-Alert([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $AlertPath -Value $line
    try {
        if (-not (Test-Path $NotifyWebhooksJson)) { Write-Log "ALERT" "no notify_webhooks.json; logged only"; return }
        $arr = Get-Content $NotifyWebhooksJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $tg = $arr | Where-Object { $_.format -eq "telegram" -and $_.enabled -ne $false -and $_.token -and $_.target } | Select-Object -First 1
        if (-not $tg) { Write-Log "ALERT" "no enabled telegram channel; logged only"; return }
        $payload = @{ chat_id = "$($tg.target)"; text = $msg; disable_web_page_preview = $true } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$($tg.token)/sendMessage" -Method Post `
            -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -ContentType "application/json; charset=utf-8" -TimeoutSec 12 | Out-Null
        Write-Log "ALERT" "telegram alert sent"
    } catch { Write-Log "ALERT" "telegram send failed: $($_.Exception.Message) (logged)" }
}

$PUB = [string]([char]0x53d1) + [string]([char]0x5e03) + [string]([char]0x94fe)  # "fa bu lian" = release chain

function Probe-ReleaseChain {
    # Returns a list of problem strings; empty = healthy.
    $problems = New-Object System.Collections.ArrayList

    # 1. latest.yml
    $yml = & curl.exe -s -m 15 "$SiteUrl/downloads/latest.yml" 2>$null | Out-String
    if (-not $yml -or $yml -notmatch "version:") { [void]$problems.Add("latest.yml unreachable/empty"); return $problems }
    $verL = if ($yml -match "(?m)^version:\s*(.+?)\s*$") { $Matches[1].Trim() } else { "" }
    $exe  = if ($yml -match "(?m)^\s*-?\s*url:\s*(.+?)\s*$") { $Matches[1].Trim() } else { "" }
    $sizeL = if ($yml -match "(?m)^\s*size:\s*(\d+)\s*$") { [long]$Matches[1] } else { 0 }
    if (-not $verL) { [void]$problems.Add("latest.yml version unparseable") }
    if (-not $exe)  { [void]$problems.Add("latest.yml exe url unparseable") }

    # 2. manifest.json version must match latest.yml (updater vs download page agree)
    $mjson = & curl.exe -s -m 15 "$SiteUrl/downloads/manifest.json" 2>$null | Out-String
    $verM = ""
    try { $verM = ([string]((ConvertFrom-Json $mjson).version)).Trim() } catch {}
    if (-not $verM) { [void]$problems.Add("manifest.json unreachable/unparseable") }
    elseif ($verL -and $verM -ne $verL) { [void]$problems.Add("version drift: manifest=$verM latest.yml=$verL") }

    # 3. the exe latest.yml points at must be downloadable and the right size
    if ($exe) {
        $head = & curl.exe -s -I -m 20 "$SiteUrl/downloads/$exe" 2>$null | Out-String
        $code = if ($head -match "HTTP/[\d.]+\s+(\d{3})") { [int]$Matches[1] } else { 0 }
        $clen = if ($head -match "(?im)^content-length:\s*(\d+)") { [long]$Matches[1] } else { -1 }
        if ($code -ne 200) { [void]$problems.Add("exe HTTP $code ($exe)") }
        elseif ($sizeL -gt 0 -and $clen -ge 0 -and $clen -ne $sizeL) {
            [void]$problems.Add("exe size mismatch: server=$clen latest.yml=$sizeL")
        }
    }

    # 4. meta-monitor the tunnel watchdog (is that scheduled task still running?)
    try {
        $age = ((Get-Date) - (Get-Item $TunnelState).LastWriteTime).TotalMinutes
        if ($age -gt $StaleMin) { [void]$problems.Add(("tunnel watchdog stale {0:N0}m (task stopped?)" -f $age)) }
    } catch { [void]$problems.Add("tunnel watchdog state missing (never ran?)") }
    # Presence via EXIT CODE (locale-proof): parsing "TaskName:"/"Status:" breaks under a non-English
    # SYSTEM locale (schtasks localizes the labels -> false "missing"; caught on first SYSTEM run).
    # A registered-but-not-running task is already covered by the stale-state check above.
    foreach ($tn in @("VisionTunnel", "VisionTunnelWatchdog")) {
        & schtasks /Query /TN $tn 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { [void]$problems.Add("scheduled task $tn missing") }
    }
    return $problems
}

# -- main ----------------------------------------------------------------------
$st = Read-State
$problems = @(Probe-ReleaseChain)

if ($problems.Count -eq 0) {
    if ($st.alerted) {
        Write-Log "OK" "release chain recovered"
        Send-Alert ("[ChatX] " + $PUB + " recovered; download/update chain healthy again.")
        $st.alerted = $false
    } else {
        Write-Log "OK" "release chain healthy"
    }
    $st.strikes = 0; Save-State $st; exit 0
}

$detail = ($problems -join "; ")
$st.strikes = [int]$st.strikes + 1
Write-Log "STRIKE" "problems ($($st.strikes)/$StrikeLimit): $detail"
if ($st.strikes -lt $StrikeLimit) { Save-State $st; exit 2 }

if ($DryRun) { Write-Log "DRYRUN" "would alert now: $detail"; Save-State $st; exit 2 }

if (-not $st.alerted) {
    Send-Alert ("[ChatX] " + $PUB + " problem(s): " + $detail +
        ". Clients may fail to download/auto-update. Check " + $SiteUrl + "/downloads/")
    $st.alerted = $true
} else {
    Write-Log "HOLD" "already alerted; still failing: $detail"
}
Save-State $st
exit 3
