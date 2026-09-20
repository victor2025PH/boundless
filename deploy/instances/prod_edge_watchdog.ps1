# prod_edge_watchdog.ps1 - end-to-end watchdog for the PRODUCTION workspace entrance.
#
# WHY: 2026-08-07 the agents' public entrance broke three times in one day while every
#   local probe stayed green (the web app answers on loopback; what breaks is the
#   DNS -> VPS nginx -> ssh reverse tunnel -> local port chain). The boss noticed before
#   any machine did. Tenant instances got edge probing inside `tenant_ops watch`; the
#   production instance (zhiliao, 18799) is OUTSIDE tenant jurisdiction by design
#   (guard_not_core), so it gets this dedicated watchdog - same doctrine as
#   vision_tunnel_watchdog.ps1 (probe the far side, two-strike, heal, then alert).
#
# WHAT (scheduled task ProdEdgeWatchdog, every 5 min):
#   leg A  tunnel : ssh into the VPS, curl http://127.0.0.1:18799/login  (ProdTunnel leg)
#   leg B  public : from THIS machine, GET https://<prod_public_url>/login (agents' path;
#                   enabled once .ops\prod_public_url.txt exists - expose_prod.ps1 writes it)
#   Verdict:
#     A ok + B ok            -> healthy (recovery alert if we had alerted)
#     A bad (ssh ok)         -> tunnel leg dead -> 2 strikes -> restart ProdTunnel -> alert if still bad
#     A ok  + B bad          -> nginx/cert/DNS leg -> no local heal -> 2 strikes -> alert
#     ssh fail + B ok        -> transient ssh; users fine -> no action
#     ssh fail + B bad       -> VPS/network down -> 2 strikes -> alert (restart would not help)
#   Restart cooldown prevents flap; state survives in .ops\prod_edge_watchdog.state.json.
#
#   leg C  relay  : GET https://relay.bd2026.cc/healthz (WeChat line, 2026-09-10). The relay is
#                   the public HTTPS front for WeCom callbacks / WeCom SSO of NAT-ed ChatX
#                   instances (relay/app.py, systemd chatx-relay on the same VPS). It has NO
#                   local heal (systemd Restart=always on the VPS side); verdict is independent
#                   of legs A/B: own 2-strike counter + own DOWN/recovered alert, same telegram
#                   tier as the katie public domain. -RelayUrl "" disables the leg.
#
# Manual: -DryRun (probe only) / -ForceRestart (drill).
# ASCII-only on purpose (PowerShell 5.1 decodes BOM-less UTF-8 as GBK).
param(
    [string]$VpsUser  = "ubuntu",
    [string]$VpsHost  = "165.154.233.121",
    [string]$Key      = "D:\chengjie-instances\.ops\vision_key",
    [int]   $Port     = 18799,
    [string]$TunnelTask = "ProdTunnel",
    [int]   $StrikeLimit = 2,
    [int]   $RestartCooldownMin = 15,
    [int]   $BootWaitSec = 90,
    # Hard process-level cap on one ssh probe (see Probe-TunnelLeg, 2026-09-11). Must stay well
    # above ConnectTimeout(15)+curl(8) so a slow-but-alive VPS is never misread as dead.
    [int]   $ProbeTimeoutSec = 30,
    [switch]$ForceRestart,
    [switch]$DryRun,
    [string]$OpsDir = "D:\chengjie-instances\.ops",
    [string]$NotifyWebhooksJson = "D:\chengjie-instances\zhiliao\data\config\notify_webhooks.json",
    [string]$RelayUrl = "https://relay.bd2026.cc/healthz"
)

$ErrorActionPreference = "SilentlyContinue"
$LogPath    = Join-Path $OpsDir "prod_edge_watchdog.log"
$StatePath  = Join-Path $OpsDir "prod_edge_watchdog.state.json"
$AlertPath  = Join-Path $OpsDir "prod_edge_watchdog.alert.log"
$MarkerPath = Join-Path $OpsDir "prod_public_url.txt"
$PidPath    = Join-Path $OpsDir "prod_tunnel.pid"
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
    # Direct Telegram via notify_webhooks.json (same channel ops configured for the site);
    # self-contained so a down python service cannot swallow the alert. Mirror to file first.
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
        $s = Get-Content $StatePath -Raw | ConvertFrom-Json
        return @{ strikes = [int]$s.strikes; last_restart_epoch = [double]$s.last_restart_epoch;
                  alerted = [bool]$s.alerted;
                  relay_strikes = [int]$s.relay_strikes; relay_alerted = [bool]$s.relay_alerted }
    } catch { return @{ strikes = 0; last_restart_epoch = 0.0; alerted = $false; relay_strikes = 0; relay_alerted = $false } }
}
function Save-State($st) { try { ($st | ConvertTo-Json -Compress) | Set-Content -Path $StatePath } catch {} }
function Get-Epoch { return [double]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) }

function Probe-TunnelLeg {
    # @{ ssh_ok; http } - http only meaningful when ssh_ok.
    # 2026-08-12 fix: marker echo decouples the remote exit code from curl's.
    # A dead tunnel port made curl exit 7 -> ssh exit 7 -> misread as "ssh
    # failed" -> outage classified as VPS/network-down ("restart would not
    # help") so the tunnel leg was never healed. ssh transport health is now
    # judged by the marker, port health by the http code alone.
    #
    # 2026-09-11 fix - two layers, both mandatory:
    #   -n (StdinNull): the in-box Windows ssh.exe (OpenSSH_for_Windows_9.5p1) has a known
    #     race (Win32-OpenSSH #1334) - a one-shot remote command that returns fast can leave the
    #     client blocked on stdin and NEVER exit, even after the VPS closed the session (VPS
    #     auth.log: Accepted + session opened within seconds, then nothing). This is what wedged
    #     this task from 09-10 09:53 to 09-11 03:53 (ssh pid 11208 with no TCP socket left;
    #     MultipleInstancesPolicy=IgnoreNew -> LastResult 0x800710E0 every 5 min, zero log lines,
    #     production entrance unmonitored for 18h) and what produced the "first probe times out,
    #     retry passes" noise in duty_watch_loop. Same launch chain A/B 60x2: 3 hangs without -n,
    #     0 with. -n removes the stdin reader entirely; stdin is never used by this probe.
    #   WaitForExit + Kill: a hung client must cost one tick, never the task. Success is judged
    #     by the marker in captured stdout (PS 5.1 .ExitCode can read $null - see uplink_watchdog).
    $cmd = "curl -s -m 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:$Port/login; echo _SSHOK_"
    $outFile = Join-Path $env:TEMP ("prod_edge_probe_" + $PID + ".txt")
    $errFile = Join-Path $env:TEMP ("prod_edge_probe_err_" + $PID + ".txt")
    $argLine = ('-n -i "{0}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 ' +
        '-o ServerAliveInterval=10 -o ServerAliveCountMax=2 {1}@{2} "{3}"') -f $Key, $VpsUser, $VpsHost, $cmd
    $txt = ""
    try {
        $p = Start-Process -FilePath "ssh" -ArgumentList $argLine -NoNewWindow -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        $null = $p.Handle
        if (-not $p.WaitForExit($ProbeTimeoutSec * 1000)) {
            try { $p.Kill() } catch {}
            Write-Log "WARN" "tunnel probe ssh exceeded ${ProbeTimeoutSec}s hard timeout; killed (client-side ssh.exe hang; see 2026-09-11 note in Probe-TunnelLeg)"
            return @{ ssh_ok = $false; http = 0 }
        }
        $txt = ((Get-Content $outFile -ErrorAction SilentlyContinue) -join " ").Trim()
    } catch {
        Write-Log "WARN" ("tunnel probe ssh launch failed: " + $_.Exception.Message)
    } finally {
        Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
    if ($txt -notmatch '_SSHOK_') { return @{ ssh_ok = $false; http = 0 } }
    $code = 0
    if ($txt -match '(\d{3})') { $code = [int]$Matches[1] }
    return @{ ssh_ok = $true; http = $code }
}

function Probe-Public {
    # returns -1 when no marker (public entrance not exposed yet), else HTTP code (0=unreachable)
    if (-not (Test-Path $MarkerPath)) { return -1 }
    $url = (Get-Content $MarkerPath -Raw).Trim().TrimEnd('/') + "/login"
    try { return [int](Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15).StatusCode }
    catch { if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode } else { return 0 } }
}

function Probe-Relay {
    # @{ http; ok; online; stale } - http 0 = unreachable; ok = body.ok true (JSON healthz of relay/app.py).
    # -1 when the leg is disabled (-RelayUrl "").
    if (-not $RelayUrl) { return @{ http = -1; ok = $false; online = -1; stale = -1 } }
    try {
        $r = Invoke-WebRequest -Uri $RelayUrl -UseBasicParsing -TimeoutSec 15
        $code = [int]$r.StatusCode
        $body = $null
        try { $body = $r.Content | ConvertFrom-Json } catch {}
        $isOk = ($code -eq 200 -and $body -and $body.ok -eq $true)
        $on = -1; $stale = -1
        if ($body) {
            if ($null -ne $body.devices_online) { $on = [int]$body.devices_online }
            if ($null -ne $body.devices_stale)  { $stale = [int]$body.devices_stale }
        }
        return @{ http = $code; ok = $isOk; online = $on; stale = $stale }
    } catch {
        $code = 0
        if ($_.Exception.Response) { try { $code = [int]$_.Exception.Response.StatusCode } catch {} }
        return @{ http = $code; ok = $false; online = -1; stale = -1 }
    }
}

function Restart-Tunnel {
    # Kill every local ssh carrying our -R (pid file first, then command-line sweep:
    # a zombie forward on the VPS releases only when the local ssh dies), then End+Run task.
    $killed = 0
    try {
        $tpid = [int](Get-Content $PidPath -Raw).Trim()
        if ($tpid) { Stop-Process -Id $tpid -Force; $killed++ }
    } catch {}
    Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
        Where-Object { $_.CommandLine -match "127\.0\.0\.1:${Port}:127\.0\.0\.1:${Port}" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $killed++ }
    schtasks /End /TN $TunnelTask | Out-Null
    Start-Sleep -Seconds 8
    schtasks /Run /TN $TunnelTask | Out-Null
    Write-Log "BOOT" "killed $killed ssh, restarted $TunnelTask, waiting up to ${BootWaitSec}s"
    $deadline = (Get-Date).AddSeconds($BootWaitSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        $t = Probe-TunnelLeg
        if ($t.ssh_ok -and $t.http -eq 200) { return $true }
    }
    return $false
}

$ALERT_DOWN = "[ChatX] PROD workspace entrance DOWN: katie public chain unhealthy and auto-heal did not recover. Agents cannot reach the workspace. Check ProdTunnel / VPS nginx / zhiliao. Log: prod_edge_watchdog.log"
$ALERT_OK   = "[ChatX] PROD workspace entrance recovered."
$RELAY_DOWN = "[ChatX] relay.bd2026.cc (WeChat relay) DOWN: /healthz unhealthy for 2 probes. WeCom callbacks / WeCom SSO of NAT-ed instances are failing. Check on VPS: systemctl status chatx-relay; nginx relay vhost; cert. Log: prod_edge_watchdog.log"
$RELAY_OK   = "[ChatX] relay.bd2026.cc (WeChat relay) recovered."

# -- main ---------------------------------------------------------------------
$st  = Read-State
$now = Get-Epoch

if ($ForceRestart) {
    Write-Log "FORCE" "manual restart drill"
    $ok = Restart-Tunnel
    $st.strikes = 0; $st.last_restart_epoch = $now; Save-State $st
    if ($ok) { Write-Log "RECOVERED" "tunnel healthy after forced restart"; exit 0 }
    Write-Log "FAILED" "tunnel not healthy after forced restart"; exit 3
}

$tun = Probe-TunnelLeg
$pub = Probe-Public

# -- leg C: WeChat relay (independent verdict; never touches tunnel strikes/restart) --------
# Runs BEFORE the tunnel verdict because every branch below exits. Own strike counter and
# own alert pair; state fields relay_strikes / relay_alerted ride in the same state file.
$rly = Probe-Relay
if ($rly.http -ne -1) {
    if ($rly.ok) {
        if ([int]$st.relay_strikes -gt 0) { Write-Log "RELAY" "recovered (http=200 ok=true online=$($rly.online) stale=$($rly.stale)), relay strikes reset" }
        else { Write-Log "RELAY" "healthy (http=200 online=$($rly.online) stale=$($rly.stale))" }
        if ($st.relay_alerted) { Send-Alert $RELAY_OK; $st.relay_alerted = $false }
        $st.relay_strikes = 0
    } else {
        $st.relay_strikes = [int]$st.relay_strikes + 1
        Write-Log "RELAYSTRIKE" "relay unhealthy ($($st.relay_strikes)/$StrikeLimit): http=$($rly.http) ok=$($rly.ok) url=$RelayUrl"
        if ($st.relay_strikes -ge $StrikeLimit) {
            if (-not $st.relay_alerted) {
                if ($DryRun) { Write-Log "DRYRUN" "would send relay DOWN alert now" }
                else { Send-Alert $RELAY_DOWN; $st.relay_alerted = $true }
            } else { Write-Log "HOLD" "relay already alerted; awaiting recovery" }
        }
    }
    Save-State $st
}

# Q-14 D-3 (#262, 2026-09-10): tunnel-vs-instance discrimination, LOG ONLY. From the VPS,
# "ssh ok but 127.0.0.1:$Port != 200" looks identical for a dead -R forward and for a
# local zhiliao restart (forward alive, nothing listening behind it). 09-09 15:18 and
# 17:13 that ambiguity made this watchdog restart a healthy tunnel. Verdict / strikes /
# restart below are untouched; this only records whether the LOCAL port had a listener,
# in this log and as one line in prod_tunnel.log where the tunnel's own drops are read.
if ($tun.ssh_ok -and $tun.http -ne 200) {
    $localListen = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($localListen.Count -eq 0) {
        $note = "local :$Port has no listener (instance restarting); tunnel_http=$($tun.http) is not tunnel death"
        Write-Log "LOCALDOWN" $note
        try {
            ("{0} [edge_watchdog] {1}" -f (Get-Date -Format s), $note) |
                Out-File (Join-Path $OpsDir "prod_tunnel.log") -Append -Encoding utf8
        } catch {}
    } else {
        Write-Log "INFO" "local :$Port is listening (pid $($localListen[0].OwningProcess)); tunnel_http=$($tun.http) points at the tunnel leg itself"
    }
}

$healthy = ($tun.ssh_ok -and $tun.http -eq 200 -and ($pub -eq -1 -or $pub -eq 200))
if ($healthy) {
    if ($st.strikes -gt 0) { Write-Log "OK" "recovered (tunnel=200 public=$pub), strikes reset" }
    else { Write-Log "OK" "healthy (tunnel=200 public=$pub)" }
    if ($st.alerted) { Send-Alert $ALERT_OK; $st.alerted = $false }
    $st.strikes = 0; Save-State $st; exit 0
}

if (-not $tun.ssh_ok -and $pub -eq 200) {
    # ssh probe path flaky but users are fine - do not touch anything
    Write-Log "WARN" "ssh probe failed but public=200; users unaffected, skipping"
    exit 0
}

$st.strikes = [int]$st.strikes + 1
$detail = "tunnel_ssh=$($tun.ssh_ok) tunnel_http=$($tun.http) public=$pub"
Write-Log "STRIKE" "unhealthy ($($st.strikes)/$StrikeLimit): $detail"
if ($st.strikes -lt $StrikeLimit) { Save-State $st; exit 2 }

# strike limit reached - classify
$tunnelLegDead = ($tun.ssh_ok -and $tun.http -ne 200)
$sinceRestartMin = if ($st.last_restart_epoch -gt 0) { ($now - $st.last_restart_epoch) / 60 } else { 1e9 }

if ($tunnelLegDead -and $sinceRestartMin -ge $RestartCooldownMin) {
    if ($DryRun) { Write-Log "DRYRUN" "would restart $TunnelTask now"; Save-State $st; exit 2 }
    Write-Log "RESTART" "tunnel leg dead confirmed -> restart"
    $ok = Restart-Tunnel
    $st.strikes = 0; $st.last_restart_epoch = $now
    if ($ok) { Write-Log "RECOVERED" "tunnel healthy after restart"; Save-State $st; exit 0 }
    Send-Alert $ALERT_DOWN; $st.alerted = $true; Save-State $st
    Write-Log "FAILED" "still unhealthy after restart"; exit 3
}

# not tunnel-heal-able (nginx/DNS/VPS down) or restart in cooldown -> alert (once per outage)
if (-not $st.alerted) { Send-Alert $ALERT_DOWN; $st.alerted = $true }
else { Write-Log "HOLD" "already alerted; awaiting recovery ($detail)" }
Save-State $st
exit 3
