# vision_tunnel_watchdog.ps1 - end-to-end watchdog for the vision reverse tunnel (117 -> VPS).
#
# WHY: 2026-08-01 06:16 the VisionTunnel task died with "remote port forwarding failed for
#   listen port 18411" looping every 10s until a manual /Run on 08-02 16:53 revived it. For ~34h
#   ALL packaged/desktop clients' image recognition silently fell back (gateway returned 502),
#   and nobody knew because the task is ONSTART-triggered with no health monitoring. Root cause was
#   NOT "task crashed" but "VPS-side port 18411 held by a zombie forward" (most likely two runner
#   instances racing the same -R port). A process-alive check would be fooled by the zombie; only
#   probing the VPS forward port end-to-end catches this.
#
# WHAT: every run (scheduled task VisionTunnelWatchdog, 5 min) SSHes into the VPS and probes both
#   reverse-forward ports (18411 -> 176, 18412 -> 140):
#     - port LISTENs on VPS AND curl /api/tags returns 200  -> that leg is healthy
#   Verdict:
#     - at least one leg healthy                 -> OK (image recognition works; single-GPU outage
#                                                   is a GPU-host problem, not the tunnel's -> WARN)
#     - both ports LISTEN but neither responds   -> zombie forward -> restart tunnel
#     - neither port LISTENs                      -> tunnel dead    -> restart tunnel
#     - SSH to VPS itself fails                   -> network issue; restarting the local tunnel would
#                                                   not help -> WARN, no restart
#   Restart: kill EVERY local ssh carrying the -R forwards (enforce single instance + reap zombies),
#   End+Run the VisionTunnel task, wait for a leg to come healthy. Restart cooldown prevents flap.
#
# State: <ops>\vision_tunnel_watchdog.state.json  {strikes, last_restart_epoch}
# Log:   <ops>\vision_tunnel_watchdog.log         (self-rotating)
# Alert: <ops>\vision_tunnel_watchdog.alert.log   (only on give-up; a human/webhook can tail this)
#
# Manual runs:
#   probe only : powershell -ExecutionPolicy Bypass -File deploy\instances\vision_tunnel_watchdog.ps1 -DryRun
#   drill      : powershell -ExecutionPolicy Bypass -File deploy\instances\vision_tunnel_watchdog.ps1 -ForceRestart
#
# NOTE: ASCII-only on purpose (PowerShell 5.1 decodes BOM-less UTF-8 as GBK and corrupts non-ASCII).

param(
    [string]$VpsUser         = "ubuntu",
    [string]$VpsHost         = "165.154.233.121",
    [string]$Key             = "D:\chengjie-instances\.ops\vision_key",
    [int]   $Port176         = 18411,
    [int]   $Port140         = 18412,
    [string]$TunnelTask      = "VisionTunnel",
    [int]   $StrikeLimit     = 2,
    [int]   $RestartCooldownMin = 15,
    [int]   $BootWaitSec     = 120,
    [switch]$ForceRestart,
    [switch]$DryRun,
    [string]$OpsDir          = "D:\chengjie-instances\.ops",
    # Single source of truth for the alert channel: reuse the site's notify_webhooks.json
    # (whatever telegram channel ops configured there). Watchdog sends Telegram DIRECTLY
    # (self-contained: works even if the python service is down), so it must NOT depend on
    # the app being up to raise an alert about the tunnel.
    [string]$NotifyWebhooksJson = "D:\chengjie-instances\zhiliao\data\config\notify_webhooks.json"
)

$ErrorActionPreference = "SilentlyContinue"
$LogPath   = Join-Path $OpsDir "vision_tunnel_watchdog.log"
$StatePath = Join-Path $OpsDir "vision_tunnel_watchdog.state.json"
$AlertPath = Join-Path $OpsDir "vision_tunnel_watchdog.alert.log"
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

function Write-Alert([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $AlertPath -Value $line
}

function Send-Alert([string]$msg) {
    # Reuse the telegram channel from notify_webhooks.json (single source of truth); send DIRECTLY
    # via the Telegram Bot API so a down python service cannot swallow the alert. Always mirror to
    # the local alert log first (survives if Telegram is unreachable).
    Write-Alert $msg
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

# Alert text is built from codepoints to keep this file ASCII (PS5.1 GBK trap).
$U = [char]0x8bc6 + [char]0x56fe  # "shi tu" (image recognition)
$ALERT_DOWN = "[ChatX] " + $U + [char]0x9694 + [char]0x79bb + " tunnel watchdog: restart did NOT recover. Image recognition is DOWN for all installed clients (gateway 502). Check 117->VPS tunnel / 176 / 140 GPU. Log: vision_tunnel_watchdog.log"
$ALERT_OK   = "[ChatX] " + $U + " tunnel recovered; image recognition back to normal."

function Read-State {
    try {
        $s = Get-Content $StatePath -Raw | ConvertFrom-Json
        return @{ strikes = [int]$s.strikes; last_restart_epoch = [double]$s.last_restart_epoch;
                  alerted = [bool]$s.alerted }
    } catch {
        return @{ strikes = 0; last_restart_epoch = 0.0; alerted = $false }
    }
}
function Save-State($st) { try { ($st | ConvertTo-Json -Compress) | Set-Content -Path $StatePath } catch {} }
function Get-Epoch { return [double]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) }

function Invoke-Ssh([string]$remoteCmd, [int]$timeoutSec = 20) {
    # Returns @{ ok; out }. ok=$false means SSH itself could not run (network/auth), NOT a remote
    # non-zero exit - we only care about stdout content for the probe.
    $args = @("-i", $Key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
              "-o", "ConnectTimeout=$timeoutSec", "$VpsUser@$VpsHost", $remoteCmd)
    try {
        $out = & ssh @args 2>$null
        return @{ ok = $true; out = ($out | Out-String).Trim() }
    } catch {
        return @{ ok = $false; out = "" }
    }
}

function Get-TunnelHealth {
    # One SSH round-trip probes both legs. For each port: is it LISTENing, and does curl /api/tags
    # (Ollama, zero-GPU) return 200 through the forward. Emits a compact machine-parsable block.
    # MUST be a single line: a multi-line here-string carries CRLF, and the CR breaks remote bash.
    $probe = "for P in $Port176 $Port140; do L=`$(ss -ltn 'sport = :'`$P 2>/dev/null | grep -c ':'`$P' '); H=`$(curl -s -m 6 -o /dev/null -w '%{http_code}' http://127.0.0.1:`$P/api/tags 2>/dev/null); echo leg `$P listen=`$L http=`$H; done"
    $r = Invoke-Ssh $probe 20
    if (-not $r.ok -or -not $r.out) { return @{ verdict = "ssh_fail"; detail = "ssh to VPS failed" } }

    $legs = @{}
    foreach ($line in ($r.out -split "`n")) {
        if ($line -match "leg (\d+) listen=(\d+) http=(\d+)") {
            $legs[[int]$Matches[1]] = @{ listen = [int]$Matches[2]; http = [int]$Matches[3] }
        }
    }
    if ($legs.Count -eq 0) { return @{ verdict = "ssh_fail"; detail = "unparseable probe: $($r.out)" } }

    $healthy = @($legs.GetEnumerator() | Where-Object { $_.Value.http -eq 200 }).Count
    $listening = @($legs.GetEnumerator() | Where-Object { $_.Value.listen -ge 1 }).Count
    $detail = ($legs.GetEnumerator() | Sort-Object Name |
        ForEach-Object { "p$($_.Key):listen=$($_.Value.listen),http=$($_.Value.http)" }) -join " "

    if ($healthy -ge 1) {
        if ($healthy -lt $legs.Count) { return @{ verdict = "warn_partial"; detail = $detail } }
        return @{ verdict = "ok"; detail = $detail }
    }
    if ($listening -ge 1) { return @{ verdict = "zombie"; detail = $detail } }  # LISTEN but dead = zombie fwd
    return @{ verdict = "dead"; detail = $detail }                              # no LISTEN = tunnel down
}

function Restart-Tunnel {
    # Enforce single instance: kill EVERY local ssh whose command line carries our -R forwards.
    # Killing the local ssh sends TCP RST to the VPS, which releases the sshd-side forward port
    # (this is the fix for 08-01's "port held by zombie" loop). Then End+Run the task.
    $killed = 0
    Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
        Where-Object { $_.CommandLine -match "$Port176:192\.168\.0\.176" -or $_.CommandLine -match "$Port140:192\.168\.0\.140" } |
        ForEach-Object { Write-Log "KILL" "stop ssh pid=$($_.ProcessId) (tunnel forward)"; Stop-Process -Id $_.ProcessId -Force; $killed++ }
    schtasks /End /TN $TunnelTask | Out-Null
    Start-Sleep -Seconds 8   # let VPS sshd release the forward ports before the new ssh binds them
    schtasks /Run /TN $TunnelTask | Out-Null
    Write-Log "BOOT" "killed $killed stale ssh, restarted $TunnelTask, waiting up to ${BootWaitSec}s"
    $deadline = (Get-Date).AddSeconds($BootWaitSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 12
        $h = Get-TunnelHealth
        if ($h.verdict -eq "ok" -or $h.verdict -eq "warn_partial") { return $true }
    }
    return $false
}

# -- main ----------------------------------------------------------------------
$st  = Read-State
$now = Get-Epoch

if ($ForceRestart) {
    Write-Log "FORCE" "manual restart drill requested"
    $ok = Restart-Tunnel
    $st.strikes = 0; $st.last_restart_epoch = $now; Save-State $st
    if ($ok) { Write-Log "RECOVERED" "tunnel healthy after forced restart"; exit 0 }
    Write-Log "FAILED" "tunnel not healthy after forced restart"; Write-Alert "vision tunnel: forced restart did NOT recover"; exit 3
}

$h = Get-TunnelHealth

switch ($h.verdict) {
    "ok" {
        if ($st.strikes -gt 0) { Write-Log "OK" "tunnel recovered ($($h.detail)), strikes reset" }
        else { Write-Log "OK" "tunnel healthy ($($h.detail))" }
        if ($st.alerted) { Send-Alert $ALERT_OK; $st.alerted = $false }
        $st.strikes = 0; Save-State $st; exit 0
    }
    "warn_partial" {
        # One GPU host is down/busy but the tunnel itself is fine and the gateway will use the live
        # leg. Restarting the tunnel would not fix a GPU-host outage, so only warn.
        Write-Log "WARN" "one leg down, tunnel OK, gateway uses live leg ($($h.detail))"
        if ($st.alerted) { Send-Alert $ALERT_OK; $st.alerted = $false }
        $st.strikes = 0; Save-State $st; exit 0
    }
    "ssh_fail" {
        # Cannot reach the VPS to probe; restarting the LOCAL tunnel would not help. Do not touch.
        Write-Log "WARN" "cannot probe VPS ($($h.detail)); skipping (network issue, not local tunnel)"
        exit 0
    }
}

# verdict is "zombie" or "dead" -> tunnel needs a restart
$st.strikes = [int]$st.strikes + 1
Write-Log "STRIKE" "tunnel $($h.verdict) ($($st.strikes)/$StrikeLimit): $($h.detail)"
if ($st.strikes -lt $StrikeLimit) { Save-State $st; exit 2 }

$sinceRestartMin = if ($st.last_restart_epoch -gt 0) { ($now - $st.last_restart_epoch) / 60 } else { 1e9 }
if ($sinceRestartMin -lt $RestartCooldownMin) {
    Write-Log "COOLDOWN" ("restart needed but last restart was {0:N0}m ago (< ${RestartCooldownMin}m), holding" -f $sinceRestartMin)
    Save-State $st; exit 2
}

if ($DryRun) { Write-Log "DRYRUN" "would restart VisionTunnel now ($($h.verdict))"; Save-State $st; exit 2 }

Write-Log "RESTART" "tunnel $($h.verdict) confirmed -> single-instance kill + reboot"
$ok = Restart-Tunnel
$st.strikes = 0; $st.last_restart_epoch = $now; Save-State $st
if ($ok) {
    Write-Log "RECOVERED" "tunnel healthy after restart"
    if ($st.alerted) { Send-Alert $ALERT_OK; $st.alerted = $false; Save-State $st }
    exit 0
}
Write-Log "FAILED" "tunnel still unhealthy after restart (next run retries)"
Send-Alert $ALERT_DOWN
$st.alerted = $true; Save-State $st
exit 3
