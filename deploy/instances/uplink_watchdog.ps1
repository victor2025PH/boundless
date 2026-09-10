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
    # v2 流量闸（首日 4 条夜间误报的修正）：只有当窗口(600s)内**其他线**合计请求
    # >= 本阈值时，静默线才计 strike——低流量时段负载均衡本就可能让健康线颗粒无收
    # （(3/4)^40 ~= 1e-5 才是「统计上不可能」的界）。流量不足=证据不足，不判且清 strike。
    [int]   $MinSiblingReq = 40,
    # v3 突然死亡窗（同晚第二层修正）：工作台是 SSE/keep-alive 长连接，LB 只分配
    # **新建连接**——请求量大不代表新连接多，闲置几小时的线可能只是没分到新连接。
    # 可靠的被动信号只有「刚才还在跑、突然断流」（13:41 PLDT 事故形态）：静默时长
    # 超过本窗（默认 30min）的线属「闲置衰减」，不可判死，不 strike（代价=错过
    # 「死了很久才被注意」的慢发现，由 SLO/坐席端回执兜底）。
    [int]   $RecentActiveMaxSec = 1800,
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
# Hard process-level timeout (2026-08-12 hotfix): the first deployment hung at
# 15:27 on a half-open ssh session (ConnectTimeout only covers the TCP connect
# phase) - the stuck instance then blocked EVERY subsequent scheduled run for 6h
# (scheduler refuses new instances while one is Running, result 0x800710E0).
# Belt: ServerAlive kills a wedged session at the ssh layer in ~20s.
# Suspenders: WaitForExit(30s) + Kill guarantees this process always exits.
# 2026-09-11: the "half-open session" guessed above was in fact the in-box Windows ssh.exe
# (9.5p1) client-side hang (Win32-OpenSSH #1334: fast one-shot command + unattended stdin ->
# client never exits; VPS had already closed the session). 20:52/21:12/21:22 on 09-10 the 30s
# kill fired three times on it. -n (StdinNull) removes the stdin reader and with it the hang
# (A/B 60x2 on this machine: 3 hangs without -n, 0 with); the kill stays as the last line.
$ipArgs = ($Uplinks -join " ")
$outFile = Join-Path $env:TEMP ("uplink_probe_" + $PID + ".txt")
$sshArgLine = ('-n -i "{0}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new ' +
    '-o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=2 ' +
    '{1}@{2} "/usr/local/bin/uplink_lastseen {3}"') -f $Key, $VpsUser, $VpsHost, $ipArgs
$errFile = Join-Path $env:TEMP ("uplink_probe_err_" + $PID + ".txt")
$out = $null
try {
    $p = Start-Process -FilePath "ssh" -ArgumentList $sshArgLine -NoNewWindow -PassThru `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    # PS 5.1 quirk: without touching .Handle while alive, .ExitCode reads $null
    # after exit ($null -eq 0 is False -> success path never taken; bit us 21:21).
    $null = $p.Handle
    if (-not $p.WaitForExit(30000)) {
        try { $p.Kill() } catch {}
        Write-Log "WARN" "probe ssh exceeded 30s hard timeout; killed (half-open session?)"
    } else {
        # Success judged by output content, not ExitCode (immune to the null quirk;
        # the helper prints only valid "<ip> <age>" lines on success).
        $out = Get-Content $outFile -ErrorAction SilentlyContinue
        if (-not $out) {
            $e1 = (Get-Content $errFile -ErrorAction SilentlyContinue | Select-Object -First 1)
            Write-Log "WARN" ("probe ssh empty (exit=" + $p.ExitCode + " err=" + $e1 + ")")
        }
    }
} catch {
    Write-Log "WARN" ("probe launch failed: " + $_.Exception.Message)
} finally {
    Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue
}
if (-not $out) {
    # Cannot reach the VPS at all -> the entrance chain is the problem, and that is
    # prod_edge_watchdog's jurisdiction. Stay quiet to avoid double alarms.
    Write-Log "WARN" "ssh probe failed; skipping (prod_edge_watchdog owns VPS-unreachable)"
    exit 0
}

$ages = @{}
$reqs = @{}
foreach ($line in @($out)) {
    # v2 三字段（age + 600s 请求数）；兼容 v1 两字段（计数按 0 = 永远过不了流量闸，
    # 等价于「助手没升级就不判」——升级顺序安全）。
    if ("$line" -match '^(\d+\.\d+\.\d+\.\d+)\s+(-?\d+)\s+(\d+)$') {
        $ages[$Matches[1]] = [int]$Matches[2]; $reqs[$Matches[1]] = [int]$Matches[3]
    } elseif ("$line" -match '^(\d+\.\d+\.\d+\.\d+)\s+(-?\d+)$') {
        $ages[$Matches[1]] = [int]$Matches[2]; $reqs[$Matches[1]] = 0
    }
}
if ($ages.Count -eq 0) { Write-Log "WARN" "unparseable probe output; skipping"; exit 0 }

$activeCount = 0
foreach ($ip in $Uplinks) {
    if ($ages.ContainsKey($ip) -and $ages[$ip] -ge 0 -and $ages[$ip] -le $ActiveSec) { $activeCount++ }
}
$totalReq = 0
foreach ($ip in $Uplinks) { if ($reqs.ContainsKey($ip)) { $totalReq += $reqs[$ip] } }
$detail = ($Uplinks | ForEach-Object {
    "{0}={1}s/{2}r" -f $_,
        $(if ($ages.ContainsKey($_)) { $ages[$_] } else { "?" }),
        $(if ($reqs.ContainsKey($_)) { $reqs[$_] } else { "?" })
}) -join " "

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
        # v2 流量闸：他线窗口请求量不足 = 「健康线也可能颗粒无收」的低流量时段，
        # 静默不构成死线证据——不 strike 且清零（半截 strike 留到高峰期会引爆误报）。
        $siblingReq = $totalReq - $(if ($reqs.ContainsKey($ip)) { $reqs[$ip] } else { 0 })
        if ($siblingReq -lt $MinSiblingReq) {
            if ($st[$ip].strikes -ne 0) { $changed = $true }
            $st[$ip].strikes = 0
            Write-Log "LOWTRAFFIC" ("uplink {0} silent (age={1}s) but siblings only carried {2} req/600s (<{3}); evidence insufficient, no strike" -f $ip, $age, $siblingReq, $MinSiblingReq)
            continue
        }
        # v3 突然死亡窗：静默太久（>RecentActiveMaxSec）= 闲置衰减形态，keep-alive
        # 拓扑下不可判死；age<0（窗口内从未见过）同属不可判。
        if ($age -lt 0 -or $age -gt $RecentActiveMaxSec) {
            if ($st[$ip].strikes -ne 0) { $changed = $true }
            $st[$ip].strikes = 0
            Write-Log "IDLE" ("uplink {0} silent beyond sudden-death window (age={1}s > {2}s); idle-decay pattern, not judgeable" -f $ip, $age, $RecentActiveMaxSec)
            continue
        }
        $st[$ip].strikes = [int]$st[$ip].strikes + 1
        $changed = $true
        Write-Log "STRIKE" ("uplink {0} silent (age={1}s, strike {2}/{3}; {4} sibling(s) active, sibling req={5}/600s)" -f $ip, $age, $st[$ip].strikes, $StrikeLimit, $activeCount, $siblingReq)
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
