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
    [switch]$ForceRestart,
    [switch]$DryRun,
    [string]$OpsDir = "D:\chengjie-instances\.ops",
    [string]$NotifyWebhooksJson = "D:\chengjie-instances\zhiliao\data\config\notify_webhooks.json"
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
                  alerted = [bool]$s.alerted }
    } catch { return @{ strikes = 0; last_restart_epoch = 0.0; alerted = $false } }
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
    $cmd = "curl -s -m 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:$Port/login; echo _SSHOK_"
    $out = ssh -i $Key -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 `
        "$VpsUser@$VpsHost" $cmd 2>$null
    $txt = ("$out").Trim()
    if ($LASTEXITCODE -ne 0 -or $txt -notmatch '_SSHOK_') { return @{ ssh_ok = $false; http = 0 } }
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
