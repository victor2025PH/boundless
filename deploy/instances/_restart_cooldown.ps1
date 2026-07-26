# _restart_cooldown.ps1 — shared restart cooldown for dual-instance ops
# Dot-source from restart_instance.ps1 / watchdog_instances.ps1.
#
# Why machine-shared path (not $LOCALAPPDATA):
#   Watchdog runs as S4U/SYSTEM; humans run as interactive user. LOCALAPPDATA
#   is per-profile, so the two sides never saw each other's cooldown → flap.
#   Canonical dir: D:\chengjie-instances\.ops\restart_cooldown\
#   Fallback: <repo>\deploy\instances\.ops\restart_cooldown\ then LOCALAPPDATA.
# ASCII-only (PS 5.1 GBK pit).

$script:RestartCooldownDefaultMin = 10

function Get-RestartCooldownDir {
    $candidates = @(
        'D:\chengjie-instances\.ops\restart_cooldown',
        (Join-Path $PSScriptRoot '.ops\restart_cooldown'),
        (Join-Path $env:LOCALAPPDATA 'boundless-instance-restart')
    )
    foreach ($d in $candidates) {
        if (-not $d) { continue }
        try {
            if (-not (Test-Path -LiteralPath $d)) {
                New-Item -ItemType Directory -Force -Path $d | Out-Null
            }
            # Prefer first writable existing/created path
            $probe = Join-Path $d '.write_probe'
            Set-Content -LiteralPath $probe -Value '1' -Encoding ASCII -ErrorAction Stop
            Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
            return $d
        } catch { continue }
    }
    return (Join-Path $env:TEMP 'boundless-instance-restart')
}

function Get-RestartCooldownPath([string]$Instance) {
    $dir = Get-RestartCooldownDir
    # Canonical name + legacy LOCALAPPDATA name for migration reads
    return @{
        primary = (Join-Path $dir ("last_restart_{0}.json" -f $Instance))
        legacy  = (Join-Path (Join-Path $env:LOCALAPPDATA 'boundless-instance-restart') ("last_restart_{0}.json" -f $Instance))
        dir     = $dir
    }
}

function Read-RestartCooldown([string]$Instance) {
    $paths = Get-RestartCooldownPath $Instance
    foreach ($p in @($paths.primary, $paths.legacy)) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        try {
            $obj = Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($obj) { return $obj }
        } catch { continue }
    }
    return $null
}

function Write-RestartCooldown {
    param(
        [Parameter(Mandatory = $true)][string]$Instance,
        [string]$DataRoot = '',
        [int]$Port = 0,
        [string]$Reason = 'restart',
        [int]$CooldownMin = 0,
        [int]$WindowSec = 0
    )
    $paths = Get-RestartCooldownPath $Instance
    # PS 5.1 `Get-Date -UFormat %s` is wrong on Windows (local-as-epoch). Use DateTimeOffset.
    $unix = [int]([DateTimeOffset]::Now.ToUnixTimeSeconds())
    $payload = [ordered]@{
        instance    = $Instance
        ts          = (Get-Date).ToString('o')
        unix        = $unix
        data_root   = $DataRoot
        port        = $Port
        reason      = $Reason
        cooldown_min = $(if ($CooldownMin -gt 0) { $CooldownMin } else { $script:RestartCooldownDefaultMin })
        window_sec  = $WindowSec
        host        = $env:COMPUTERNAME
        source      = $Reason
    } | ConvertTo-Json -Compress
    # UTF-8 no BOM — Python json.loads rejects utf-8-with-BOM unless utf-8-sig
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($paths.primary, $payload, $utf8)
    try {
        $legDir = Split-Path -Parent $paths.legacy
        if ($legDir -and -not (Test-Path $legDir)) { New-Item -ItemType Directory -Force -Path $legDir | Out-Null }
        if ($paths.legacy -ne $paths.primary) {
            [System.IO.File]::WriteAllText($paths.legacy, $payload, $utf8)
        }
    } catch {}
    # Phase6: append event log for flap detection (ops + watchdog alerts)
    $flap = Add-RestartEventAndTestFlap -Instance $Instance -Reason $Reason -Unix $unix -Dir $paths.dir
    return @{ path = $paths.primary; flap = $flap }
}

function Add-RestartEventAndTestFlap {
    param(
        [Parameter(Mandatory = $true)][string]$Instance,
        [string]$Reason = 'restart',
        [int]$Unix = 0,
        [string]$Dir = '',
        [int]$WindowMin = 30,
        [int]$Threshold = 2
    )
    if (-not $Dir) { $Dir = (Get-RestartCooldownPath $Instance).dir }
    $tsUnix = $Unix
    if ($tsUnix -le 0) { $tsUnix = [int]([DateTimeOffset]::Now.ToUnixTimeSeconds()) }
    $evPath = Join-Path $Dir 'restart_events.jsonl'
    $line = (@{
        unix = $tsUnix; instance = $Instance; reason = $Reason; host = $env:COMPUTERNAME
        ts = (Get-Date).ToString('o')
    } | ConvertTo-Json -Compress)
    try {
        $utf8 = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::AppendAllText($evPath, ($line + "`n"), $utf8)
        # Cap file ~400 lines (keep tail)
        $all = @(Get-Content -LiteralPath $evPath -ErrorAction SilentlyContinue)
        if ($all.Count -gt 400) {
            $tail = $all[($all.Count - 300)..($all.Count - 1)]
            [System.IO.File]::WriteAllLines($evPath, $tail, $utf8)
        }
    } catch {}
    return (Test-RestartFlap -Instance $Instance -Dir $Dir -WindowMin $WindowMin -Threshold $Threshold -NowUnix $tsUnix)
}

function Test-RestartFlap {
    param(
        [Parameter(Mandatory = $true)][string]$Instance,
        [string]$Dir = '',
        [int]$WindowMin = 30,
        [int]$Threshold = 2,
        [int]$NowUnix = 0
    )
    if (-not $Dir) { $Dir = (Get-RestartCooldownPath $Instance).dir }
    if ($NowUnix -le 0) { $NowUnix = [int]([DateTimeOffset]::Now.ToUnixTimeSeconds()) }
    $evPath = Join-Path $Dir 'restart_events.jsonl'
    $count = 0
    $reasons = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $evPath) {
        $cut = $NowUnix - ($WindowMin * 60)
        foreach ($raw in @(Get-Content -LiteralPath $evPath -ErrorAction SilentlyContinue)) {
            if (-not $raw) { continue }
            try {
                $o = $raw | ConvertFrom-Json
                if ([string]$o.instance -ne $Instance) { continue }
                $u = 0
                try { $u = [int]$o.unix } catch {}
                if ($u -ge $cut) {
                    $count++
                    if ($o.reason) { [void]$reasons.Add([string]$o.reason) }
                }
            } catch { continue }
        }
    }
    return @{
        flapping   = ($count -ge $Threshold)
        count      = $count
        threshold  = $Threshold
        window_min = $WindowMin
        reasons    = @($reasons | Select-Object -Last 5)
        path       = $evPath
    }
}

function Test-RestartCooldownActive {
    param(
        [Parameter(Mandatory = $true)][string]$Instance,
        [int]$CooldownMin = 0
    )
    if ($CooldownMin -lt 0) { $CooldownMin = 0 }
    if ($CooldownMin -eq 0) { $CooldownMin = $script:RestartCooldownDefaultMin }
    $cd = Read-RestartCooldown $Instance
    if (-not $cd -or -not $cd.ts) {
        return @{ active = $false; record = $null; left_min = 0; path = (Get-RestartCooldownPath $Instance).primary }
    }
    # Per-record cooldown_min overrides caller default when present
    $needMin = $CooldownMin
    try {
        if ($cd.cooldown_min -and [int]$cd.cooldown_min -gt 0) { $needMin = [int]$cd.cooldown_min }
    } catch {}
    try {
        $last = [datetime]::Parse([string]$cd.ts, $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
        $elapsed = (Get-Date) - $last
        $need = [TimeSpan]::FromMinutes($needMin)
        if ($elapsed -lt $need) {
            $left = [int][Math]::Ceiling(($need - $elapsed).TotalMinutes)
            if ($left -lt 1) { $left = 1 }
            return @{
                active  = $true
                record  = $cd
                left_min = $left
                elapsed_min = [int]$elapsed.TotalMinutes
                path    = (Get-RestartCooldownPath $Instance).primary
            }
        }
    } catch {}
    return @{ active = $false; record = $cd; left_min = 0; path = (Get-RestartCooldownPath $Instance).primary }
}

# ── Restart-inflight sentinel (2026-07-26 ghost-instance fix) ────────────────
# restart_instance stops the port ~40-60s before the new process binds. The
# watchdog DOWN heal path deliberately ignores the cooldown ("process is dead
# anyway"), so a watchdog tick inside that window started a SECOND engine on
# the same data root: it lost the port race (bind 10048) but its Telegram
# client kept running — a ghost fighting the real instance for the session
# (same class as the 2026-07-22 incident). The sentinel marks "a restart is
# being orchestrated right now" so the watchdog skips healing inside the
# window. TTL self-expires if the restart script crashes mid-run.

function Get-RestartInflightPath([string]$Instance) {
    return (Join-Path (Get-RestartCooldownDir) ("restart_inflight_{0}.json" -f $Instance))
}

function Write-RestartInflight {
    param(
        [Parameter(Mandatory = $true)][string]$Instance,
        [string]$Reason = 'restart',
        [int]$TtlSec = 330
    )
    $p = Get-RestartInflightPath $Instance
    $payload = [ordered]@{
        instance   = $Instance
        ts         = (Get-Date).ToString('o')
        unix       = [int]([DateTimeOffset]::Now.ToUnixTimeSeconds())
        reason     = $Reason
        ttl_sec    = $TtlSec
        host       = $env:COMPUTERNAME
        script_pid = $PID
    } | ConvertTo-Json -Compress
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($p, $payload, $utf8)
    return $p
}

function Clear-RestartInflight([string]$Instance) {
    $p = Get-RestartInflightPath $Instance
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    }
}

function Test-RestartInflight([string]$Instance) {
    $p = Get-RestartInflightPath $Instance
    if (-not (Test-Path -LiteralPath $p)) {
        return @{ active = $false; record = $null; age_sec = -1; path = $p }
    }
    try {
        $o = Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json
        $ttl = 330
        try { if ($o.ttl_sec -and [int]$o.ttl_sec -gt 0) { $ttl = [int]$o.ttl_sec } } catch {}
        $age = [int](([DateTimeOffset]::Now.ToUnixTimeSeconds()) - [int]$o.unix)
        if ($age -ge 0 -and $age -lt $ttl) {
            return @{ active = $true; record = $o; age_sec = $age; path = $p }
        }
    } catch {}
    # Stale (past TTL) or unreadable -> treat as not active (self-heal)
    return @{ active = $false; record = $null; age_sec = -1; path = $p }
}

function Get-ChengjieDirtyRestartAdvice {
    # Returns: must_restart | hot_only | clean | unknown
    param([string]$EngineDir = '')
    if (-not $EngineDir) {
        $EngineDir = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'engines\chengjie'
    }
    if (-not (Test-Path (Join-Path $EngineDir '.git')) -and -not (Test-Path (Join-Path (Split-Path -Parent (Split-Path -Parent $EngineDir)) '.git'))) {
        return @{ kind = 'unknown'; py = @(); hot = @(); other = @() }
    }
    Push-Location $EngineDir
    try {
        $lines = @(git status --porcelain 2>$null)
    } catch { $lines = @() }
    finally { Pop-Location }
    $py = New-Object System.Collections.Generic.List[string]
    $hot = New-Object System.Collections.Generic.List[string]
    $other = New-Object System.Collections.Generic.List[string]
    foreach ($raw in $lines) {
        if (-not $raw -or $raw.Length -lt 4) { continue }
        $path = $raw.Substring(3).Trim().Trim('"')
        if ($path -match ' -> ') { $path = ($path -split ' -> ')[-1] }
        $p = $path -replace '\\', '/'
        if ($p -match '\.py$' -or $p -eq 'main.py' -or $p -match '(^|/)bootstrap/') {
            $py.Add($p) | Out-Null
        } elseif (
            $p -match '(^|/)src/web/templates/' -or
            $p -match '(^|/)src/web/i18n_packs/' -or
            $p -match '(^|/)src/web/web_i18n\.py$' -or
            $p -match 'config\.local\.yaml$' -or
            $p -match '\.(md|css|svg|png|jpg)$'
        ) {
            $hot.Add($p) | Out-Null
        } else {
            $other.Add($p) | Out-Null
        }
    }
    $kind = 'clean'
    if ($py.Count -gt 0 -or $other.Count -gt 0) { $kind = 'must_restart' }
    elseif ($hot.Count -gt 0) { $kind = 'hot_only' }
    return @{ kind = $kind; py = @($py); hot = @($hot); other = @($other) }
}
