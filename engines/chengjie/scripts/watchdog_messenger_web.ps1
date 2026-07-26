# watchdog_messenger_web.ps1 - watchdog for the Messenger web (Playwright) Node service (:8791)
#
# WHY: 2026-07-26 - the service died overnight and nobody noticed until an agent tried to connect
#   an account and got "connection service not running". Messenger had NO autostart task and NO
#   watchdog at all (WhatsApp had both), so this link had effectively never stayed online.
#
# DIFFERENCE vs watchdog_wa_baileys.ps1 (deliberate): the WA watchdog exits early when the
#   orchestrator manages zero accounts, so it never guards mere process liveness. Messenger is
#   normally in exactly that state before the first login - which is precisely when the process
#   died. So this watchdog checks liveness FIRST (independent of account count), then does the
#   same business-level expected-vs-actual comparison once accounts exist.
#
# WHAT: every run (scheduled task, 5 min cadence):
#   1. GET <node>/health (3s). Unreachable -> liveness miss (regardless of account count).
#   2. Node alive + main site reachable -> expected set = orchestrator accounts with
#      platform=messenger; any expected account missing from <node>/accounts -> business miss
#      ("half-dead": HTTP fine but sessions gone). Zero expected accounts -> healthy idle, the
#      normal state while waiting for the first login.
#   3. missStreak >= MissLimit (2 rounds ~ 10 min) -> hard restart: kill the node.exe owning
#      port 8791 (verified by process name AND command line containing messenger-web\server.js -
#      never kill a foreign process) + leftover messenger-web node processes, then
#      schtasks /Run /TN "Messenger-Web-Service". 30 min restart cooldown (inside cooldown:
#      alert only, no restart).
#   4. Self-heal actions POST an alert to the GROUP TG RELAY on the VPS (<AlertBase>/api/ops/alert,
#      Bearer = EVENT_INGEST_KEY machine env) - the SAME relay watchdog_wa_baileys.ps1 uses.
#      The main site (:18799) has NO /api/ops/alert route. No key -> log-only, never fail.
#
# NOTE: there is no gentle per-account reconnect path here (unlike WA): a Messenger session is a
#   whole browser context, and the server already self-heals context crashes internally
#   (scheduleRecovery). If it got this far, the in-process recovery has already lost.
#
# State:  logs\watchdog_messenger_web.state.json   {missStreak, lastRestartTs}
# Log:    logs\watchdog_messenger_web.log          (rotates at >2MB, keeps one .1 copy)
#
# Manual runs:
#   probe only : powershell -ExecutionPolicy Bypass -File scripts\watchdog_messenger_web.ps1 -DryRun
#   normal     : powershell -ExecutionPolicy Bypass -File scripts\watchdog_messenger_web.ps1
#
# -DryRun probes and prints every decision but performs NO self-heal, NO alerts and NO state writes
# (safe to run while the scheduled task is active).
#
# Exit code is always 0 (scheduled-task semantics).
#
# NOTE: ASCII-only on purpose (PowerShell 5.1 decodes BOM-less UTF-8 as GBK and corrupts CJK
#   literals; same convention as watchdog_wa_baileys.ps1 / watchdog_emotion_tts.ps1).

param(
    [string]$MainBase           = "http://127.0.0.1:18799",
    [string]$NodeBase           = "http://127.0.0.1:8791",
    [string]$AlertBase          = "",
    [string]$IngestKey          = "",
    [string]$DataDir            = "D:\chengjie-instances\zhiliao\data",
    [string]$TaskName           = "Messenger-Web-Service",
    [int]   $MissLimit          = 2,
    [int]   $RestartCooldownMin = 30,
    [int]   $MainTimeoutSec     = 5,
    [int]   $NodeTimeoutSec     = 3,
    [switch]$DryRun,
    [string]$LogPath            = "$PSScriptRoot\..\logs\watchdog_messenger_web.log",
    [string]$StatePath          = "$PSScriptRoot\..\logs\watchdog_messenger_web.state.json"
)

$ErrorActionPreference = "SilentlyContinue"
$NodePort = ([Uri]$NodeBase).Port

# resolve alert relay + key (param > env; machine-level env is inherited by scheduled tasks)
if (-not $AlertBase) { $AlertBase = [string]$env:PERSONA_SYNC_BASE }
if (-not $AlertBase) { $AlertBase = "https://bd2026.cc" }
$AlertBase = $AlertBase.TrimEnd("/")
if (-not $IngestKey) { $IngestKey = [string]$env:EVENT_INGEST_KEY }

# -- log / state helpers -----------------------------------------------------
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
try {
    if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt 2MB)) {
        Move-Item -Force $LogPath ($LogPath + ".1")
    }
} catch {}

function Write-Log([string]$level, [string]$msg) {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    # UTF8: .NET exception text is localized (e.g. Chinese on zh-CN hosts); the PS5.1
    # default ANSI writer mangles it into unreadable bytes.
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Read-State {
    try {
        $s = Get-Content $StatePath -Raw | ConvertFrom-Json
        return @{ missStreak = [int]$s.missStreak; lastRestartTs = [double]$s.lastRestartTs }
    } catch {
        return @{ missStreak = 0; lastRestartTs = 0.0 }
    }
}

function Save-State($st) {
    if ($DryRun) { return }
    try { ($st | ConvertTo-Json -Compress) | Set-Content -Path $StatePath } catch {}
}

function Get-Epoch { return [double]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) }

# -- probes ------------------------------------------------------------------
function Get-AuthToken {
    # same resolution as services/messenger-web/start.ps1::Get-CfgToken:
    # overlay (config.local.yaml) first, then config.yaml; last auth_token line wins.
    $files = @(
        (Join-Path $DataDir "config\config.local.yaml"),
        (Join-Path $DataDir "config\config.yaml")
    )
    foreach ($f in $files) {
        if (Test-Path $f) {
            $m = Select-String -Path $f -Pattern '^\s*auth_token:\s*(\S+)' | Select-Object -Last 1
            if ($m) { return $m.Matches[0].Groups[1].Value.Trim('"').Trim("'") }
        }
    }
    return ""
}

function Test-NodeAlive {
    # liveness = /health answers. @{ok; err}
    try {
        $r = Invoke-RestMethod -Uri "$NodeBase/health" -TimeoutSec $NodeTimeoutSec -ErrorAction Stop
        if ($r -and $r.ok) { return @{ ok = $true } }
        return @{ ok = $false; err = "health returned not-ok" }
    } catch {
        return @{ ok = $false; err = "$($_.Exception.Message)" }
    }
}

function Get-ExpectedAccounts([string]$token) {
    # expected set = orchestrator-managed messenger account_ids. @{ok; ids; err}
    try {
        $r = Invoke-RestMethod -Uri "$MainBase/api/accounts/orchestrator" `
            -Headers @{ Authorization = "Bearer $token" } `
            -TimeoutSec $MainTimeoutSec -ErrorAction Stop
        if (-not $r -or -not $r.ok) { return @{ ok = $false; err = "response not ok" } }
        $ids = @()
        foreach ($a in @($r.accounts)) {
            if ("$($a.platform)" -eq "messenger" -and "$($a.account_id)" -ne "") {
                $ids += "$($a.account_id)"
            }
        }
        return @{ ok = $true; ids = @($ids | Select-Object -Unique) }
    } catch {
        return @{ ok = $false; err = "$($_.Exception.Message)" }
    }
}

function Get-NodeAccounts {
    # actual set = authorized accounts reported by the Node service. @{ok; ids; err}
    try {
        $r = Invoke-RestMethod -Uri "$NodeBase/accounts" -TimeoutSec $NodeTimeoutSec -ErrorAction Stop
        $ids = @()
        foreach ($a in @($r.accounts)) {
            if ("$($a.account_id)" -ne "") { $ids += "$($a.account_id)" }
        }
        return @{ ok = $true; ids = @($ids | Select-Object -Unique) }
    } catch {
        return @{ ok = $false; ids = @(); err = "$($_.Exception.Message)" }
    }
}

function Send-Alert([string]$text) {
    # best-effort; target is the VPS group TG relay (AlertBase + EVENT_INGEST_KEY), NOT the main site.
    if ($DryRun) { Write-Log "DRYRUN" "would alert: $text"; return }
    if (-not $IngestKey) { Write-Log "WARN" "no EVENT_INGEST_KEY - alert logged only: $text"; return }
    try {
        $body = @{ text = $text; source = "messenger-web-watchdog@$env:COMPUTERNAME" } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "$AlertBase/api/ops/alert" -Method Post `
            -Headers @{ Authorization = "Bearer $IngestKey" } -ContentType "application/json" `
            -Body $body -TimeoutSec 10 -ErrorAction Stop | Out-Null
    } catch {
        Write-Log "WARN" "alert post failed (ignored): $($_.Exception.Message)"
    }
}

# -- self-heal primitives ----------------------------------------------------
function Get-PortOwnerPids {
    $result = @()
    $lines = @(netstat -ano | Select-String -Pattern "LISTENING")
    foreach ($ln in $lines) {
        if ("$ln" -match ('^\s*TCP\s+\S+:' + $NodePort + '\s+\S+\s+LISTENING\s+(\d+)\s*$')) {
            $result += [int]$Matches[1]
        }
    }
    return @($result | Where-Object { $_ -gt 0 } | Select-Object -Unique)
}

function Get-ProcInfo([int]$procId) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId"
    if (-not $p) { return $null }
    return @{ name = "$($p.Name)"; cmd = "$($p.CommandLine)" }
}

function Test-IsMessengerNode($info) {
    # anti-friendly-fire: must be node.exe AND command line must reference messenger-web server.js
    if (-not $info) { return $false }
    if ($info.name -ne "node.exe") { return $false }
    return (($info.cmd -match "messenger-web") -and ($info.cmd -match "server\.js"))
}

function Invoke-HardRestart {
    # kill verified port owner + leftover messenger-web node processes, then re-launch via task.
    # Returns @{ok; killed; reason}.
    $killed = @()
    foreach ($procId in (Get-PortOwnerPids)) {
        $info = Get-ProcInfo $procId
        if (Test-IsMessengerNode $info) {
            Write-Log "KILL" "stop pid=$procId (port $NodePort owner, verified messenger-web node.exe)"
            Stop-Process -Id $procId -Force
            $killed += $procId
        } else {
            $nm = "unknown"
            if ($info) { $nm = $info.name }
            return @{ ok = $false; killed = $killed;
                      reason = "port $NodePort owner pid=$procId is '$nm' (cmdline does not match messenger-web\server.js) - refusing to kill" }
        }
    }
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
        Where-Object { ("$($_.CommandLine)" -match "messenger-web") -and ("$($_.CommandLine)" -match "server\.js") } |
        ForEach-Object {
            Write-Log "KILL" "stop leftover messenger-web node pid=$($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force
            $killed += [int]$_.ProcessId
        }
    Start-Sleep -Seconds 3
    # /End first: the task wrapper (powershell running start.ps1) may still be winding down and
    # default task policy ignores /Run while an instance is marked running.
    schtasks /End /TN $TaskName 2>&1 | Out-Null
    schtasks /Run /TN $TaskName 2>&1 | Out-Null
    Write-Log "BOOT" "triggered scheduled task $TaskName"
    return @{ ok = $true; killed = @($killed | Select-Object -Unique) }
}

# -- main --------------------------------------------------------------------
$st  = Read-State
$now = Get-Epoch

# 1) liveness first - this is the gap the WA watchdog leaves open (it exits on zero accounts).
$alive = Test-NodeAlive
$missReason = ""

if (-not $alive.ok) {
    $missReason = "node :$NodePort not answering /health ($($alive.err))"
} else {
    # 2) alive: compare orchestrator-expected vs node-actual (catches the half-dead case)
    $token = Get-AuthToken
    if ($token -eq "") {
        Write-Log "OK" "node :$NodePort alive; auth_token not found under $DataDir\config - skipping business check"
        $st.missStreak = 0
        Save-State $st
        exit 0
    }
    $exp = Get-ExpectedAccounts $token
    if (-not $exp.ok) {
        # main site down: its own watchdog owns that problem. Node is alive, so we are done.
        Write-Log "OK" "node :$NodePort alive; main site not available ($($exp.err)) - business check skipped"
        $st.missStreak = 0
        Save-State $st
        exit 0
    }
    if (@($exp.ids).Count -eq 0) {
        # normal state before the first login: service idles waiting for someone to connect
        if ($st.missStreak -ne 0) { $st.missStreak = 0; Save-State $st }
        Write-Log "OK" "node :$NodePort alive; orchestrator manages no messenger accounts (idle, waiting for first login)"
        exit 0
    }
    $act = Get-NodeAccounts
    $missing = @($exp.ids | Where-Object { @($act.ids) -notcontains $_ })
    if ($act.ok -and $missing.Count -eq 0) {
        if ($st.missStreak -gt 0) {
            Write-Log "OK" "all expected accounts back ($($exp.ids -join ',')), streak reset"
        } else {
            Write-Log "OK" "all expected accounts present ($($exp.ids -join ','))"
        }
        $st.missStreak = 0
        Save-State $st
        exit 0
    }
    $missReason = "half-dead: /health ok but missing=[$($missing -join ',')] expected=[$($exp.ids -join ',')] actual=[$(@($act.ids) -join ',')]"
}

$newStreak = [int]$st.missStreak + 1
Write-Log "MISS" "$missReason streak=$newStreak/$MissLimit"

if ($DryRun) {
    if ($newStreak -lt $MissLimit) {
        Write-Log "DRYRUN" "below miss limit - would only record streak and exit"
        exit 0
    }
    $sinceMin = 1e9
    if ($st.lastRestartTs -gt 0) { $sinceMin = ($now - $st.lastRestartTs) / 60 }
    if ($sinceMin -lt $RestartCooldownMin) {
        Write-Log "DRYRUN" ("would skip restart (cooldown: last restart {0:N0}m ago < ${RestartCooldownMin}m), alert only" -f $sinceMin)
    } else {
        Write-Log "DRYRUN" "would hard-restart via task $TaskName"
    }
    exit 0
}

$st.missStreak = $newStreak
Save-State $st

if ($newStreak -lt $MissLimit) { exit 0 }

$sinceRestartMin = 1e9
if ($st.lastRestartTs -gt 0) { $sinceRestartMin = ($now - $st.lastRestartTs) / 60 }
if ($sinceRestartMin -lt $RestartCooldownMin) {
    Write-Log "COOLDOWN" ("still bad but last restart was {0:N0}m ago (< ${RestartCooldownMin}m) - restart skipped" -f $sinceRestartMin)
    Send-Alert ("[messenger-web] $missReason (streak $newStreak) but restart is in cooldown ({0:N0}m/{1}m) - skipped, manual check advised." -f $sinceRestartMin, $RestartCooldownMin)
    exit 0
}

Write-Log "RESTART" "hard restart: kill port $NodePort owner + leftovers, then schtasks /Run $TaskName"
$r = Invoke-HardRestart
if ($r.ok) {
    $st.missStreak = 0
    $st.lastRestartTs = $now
    Save-State $st
    Send-Alert "[messenger-web] hard restart executed: killed node pid(s) [$($r.killed -join ',')], re-launched via task $TaskName ($missReason)."
    Write-Log "RESTART" "done (killed: [$($r.killed -join ',')]); next rounds re-verify"
} else {
    Write-Log "REFUSE" $r.reason
    Send-Alert "[messenger-web] wanted to hard-restart but $($r.reason). Manual intervention needed."
}
exit 0
