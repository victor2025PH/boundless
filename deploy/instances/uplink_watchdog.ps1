# uplink_watchdog.ps1 - per-broadband-line health monitor for the office multi-WAN.
#
# WHY (2026-08-12 reliability review P1-6): the office egresses through 4
#   load-balanced broadband uplinks. When ONE line stalls (13:41 that day: PLDT
#   122.54.137.215 silent for 3 minutes while the other three kept flowing),
#   seats whose HTTPS flows were pinned to it saw the red "disconnected" banner
#   - and diagnosing it took hours of forensic nginx-log work. This watchdog
#   turns that exact forensic method into a 5-minute automatic check: it asks
#   the VPS for "seconds since last request" per known office uplink IP, and
#   alerts when a line is silent while its siblings are demonstrably active.
#
# HOW: ssh -> /usr/local/bin/uplink_lastseen <ips> (helper installed on the VPS;
#   parses nginx access.log timestamps server-side, ASCII output "<ip> <age>").
#   Verdict per uplink: active (age <= ActiveSec) / silent (age > SilentSec or
#   never seen). An uplink strikes only while >=1 sibling is ACTIVE (all-quiet =
#   night shift, not an outage). StrikeLimit consecutive ticks -> Telegram alert
#   (same self-contained channel pattern as prod_edge_watchdog / vision watchdog:
#   notify_webhooks.json read directly, a down python service cannot swallow it).
#   Recovery -> one recovery notice, state reset.
#
# Scheduled task (SYSTEM, every 5 min):
#   schtasks /Create /TN UplinkWatchdog /SC MINUTE /MO 5 /RU SYSTEM /F /TR "powershell -NoProfile -ExecutionPolicy Bypass -File D:\boundless\deploy\instances\uplink_watchdog.ps1"
#
# Manual: -DryRun (probe + classify only, no alert/state write).
# If the office ISP set changes, update -Uplinks (and the VPS-side helper needs
# nothing - it takes IPs as arguments).
# ASCII-only on purpose (PowerShell 5.1 decodes BOM-less UTF-8 as GBK).

param(
    [string[]]$Uplinks = @("27.126.158.220", "112.198.239.199", "122.53.52.79", "122.54.137.215"),
    [int]   $SilentSec   = 360,
    [int]   $ActiveSec   = 180,
    [int]   $StrikeLimit = 2,
    [string]$VpsUser = "ubuntu",
    [string]$VpsHost = "165.154.233.121",
    [string]$Key     = "D:\chengjie-instances\.ops\vision_key",
    [switch]$DryRun,
    [string]$OpsDir  = "D:\chengjie-instances\.ops",
    [string]$NotifyWebhooksJson = "D:\chengjie-instances\zhiliao\data\config\notify_webhooks.json"
)

$ErrorActionPreference = "SilentlyContinue"
$LogPath   = Join-Path $OpsDir "uplink_watchdog.log"
$StatePath = Join-Path $OpsDir "uplink_watchdog.state.json"
$AlertPath = Join-Path $OpsDir "uplink_watchdog.alert.log"
New-Item -ItemType Directory -Force -Path $OpsDir | Out-Null

function Write-Log([string]$level, [string]$msg) {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
    try {
        $c = @(Get-Content $LogPath)
        if ($c.Count -gt 3000) { Set-Content -Path $LogPath -Value ($c[-2000..-1]) }
    } catch {}
}

function Send-Alert([string]$msg) {
    Add-Content -Path $AlertPath -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg)
    try {
        if (-not (Test-Path $NotifyWebhooksJson)) { Write-Log "ALERT" "no notify_webhooks.json; logged only"; return }
        $arr = Get-Content $NotifyWebhooksJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $tg = $arr | Where-Object { $_.format -eq "telegram" -and $_.enabled -ne $false -and $_.token -and $_.target } | Select-Object -First 1
        if (-not $tg) { Write-Log "ALERT" "no enabled telegram channel; logged only"; return }
        $payload = @{ chat_id = "$($tg.target)"; text = $msg; disable_web_page_preview = $true } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$($tg.token)/sendMessage" -Method Post `
            -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -ContentType "application/json; charset=utf-8" -TimeoutSec 12 | Out-Null
        Write-Log "ALERT" "telegram alert sent"
    } catch {
        Write-Log "ALERT" "telegram send failed: $($_.Exception.Message) (logged to alert file)"
    }
}

function Read-State {
    try {
        $raw = Get-Content $StatePath -Raw | ConvertFrom-Json
        $m = @{}
        foreach ($p in $raw.PSObject.Properties) {
            $m[$p.Name] = @{ strikes = [int]$p.Value.strikes; alerted = [bool]$p.Value.alerted }
        }
        return $m
    } catch { return @{} }
}
function Save-State($st) {
    try { ($st | ConvertTo-Json -Compress) | Set-Content -Path $StatePath } catch {}
}

# -- probe ---------------------------------------------------------------------
$ipArgs = ($Uplinks -join " ")
$out = & ssh "-i" $Key "-o" "BatchMode=yes" "-o" "StrictHostKeyChecking=accept-new" `
    "-o" "ConnectTimeout=15" "$VpsUser@$VpsHost" "/usr/local/bin/uplink_lastseen $ipArgs" 2>$null
if (-not $out) {
    # Cannot reach the VPS at all -> the entrance chain is the problem, and that is
    # prod_edge_watchdog's jurisdiction. Stay quiet to avoid double alarms.
    Write-Log "WARN" "ssh probe failed; skipping (prod_edge_watchdog owns VPS-unreachable)"
    exit 0
}

$ages = @{}
foreach ($line in @($out)) {
    if ("$line" -match '^(\d+\.\d+\.\d+\.\d+)\s+(-?\d+)$') { $ages[$Matches[1]] = [int]$Matches[2] }
}
if ($ages.Count -eq 0) { Write-Log "WARN" "unparseable probe output; skipping"; exit 0 }

$activeCount = 0
foreach ($ip in $Uplinks) {
    if ($ages.ContainsKey($ip) -and $ages[$ip] -ge 0 -and $ages[$ip] -le $ActiveSec) { $activeCount++ }
}
$detail = ($Uplinks | ForEach-Object { "{0}={1}s" -f $_, $(if ($ages.ContainsKey($_)) { $ages[$_] } else { "?" }) }) -join " "

if ($activeCount -eq 0) {
    # Everything quiet (night shift / no seats online). Not judgeable - do not
    # strike, do not reset alerted flags (a dead line should not "recover" just
    # because the office went home).
    Write-Log "QUIET" "no uplink active ($detail); skipping judgement"
    exit 0
}

$st = Read-State
$changed = $false
foreach ($ip in $Uplinks) {
    $age = if ($ages.ContainsKey($ip)) { $ages[$ip] } else { -1 }
    $silent = ($age -lt 0 -or $age -gt $SilentSec)
    if (-not $st.ContainsKey($ip)) { $st[$ip] = @{ strikes = 0; alerted = $false } }

    if ($silent) {
        $st[$ip].strikes = [int]$st[$ip].strikes + 1
        $changed = $true
        Write-Log "STRIKE" ("uplink {0} silent (age={1}s, strike {2}/{3}; {4} sibling(s) active)" -f $ip, $age, $st[$ip].strikes, $StrikeLimit, $activeCount)
        if ($st[$ip].strikes -ge $StrikeLimit -and -not $st[$ip].alerted) {
            if ($DryRun) {
                Write-Log "DRYRUN" ("would alert: uplink {0} down" -f $ip)
            } else {
                Send-Alert ("[ChatX] office broadband line {0} looks DOWN: no requests for {1}+ min while other lines flow. Seats pinned to it will see disconnect banners - office seats can switch to the LAN entrance http://192.168.0.117:18799 . Log: uplink_watchdog.log" -f $ip, [int]($SilentSec / 60))
                $st[$ip].alerted = $true
            }
        }
    } else {
        if ($st[$ip].alerted) {
            if ($DryRun) { Write-Log "DRYRUN" ("would send recovery for {0}" -f $ip) }
            else {
                Send-Alert ("[ChatX] office broadband line {0} recovered (traffic flowing again)." -f $ip)
                $st[$ip].alerted = $false
            }
        }
        if ($st[$ip].strikes -ne 0) { $changed = $true }
        $st[$ip].strikes = 0
    }
}
if (-not $DryRun) { Save-State $st } elseif ($changed) { Write-Log "DRYRUN" "state not written" }
Write-Log "OK" ("tick done: {0} active / {1} watched ({2})" -f $activeCount, $Uplinks.Count, $detail)
exit 0
