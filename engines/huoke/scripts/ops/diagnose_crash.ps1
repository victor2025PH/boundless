# OpenClaw crash diagnosis
# Investigates server.py crashes by correlating service_wrapper.log
# (records exit events) with logs/openclaw.log (records stack traces /
# unhandled exceptions just before exit).
#
# Args:
#   -Last N      Look at last N crash events. Default 5.
#   -Days N      Only look at events in the last N days. Default 1.
#   -Hours N     Only look at events in the last N hours (overrides -Days).
#   -Json        Structured output.
#
# Usage:
#   diagnose_crash.bat                  # last 5 crashes in 1 day
#   diagnose_crash.bat -Hours 1         # last hour only
#   diagnose_crash.bat -Last 10 -Days 7 # last 10 in 7 days
#   diagnose_crash.bat -Json

param(
    [int]$Last = 5,
    [int]$Days = 1,
    [int]$Hours = 0,
    [switch]$Json
)

# -Hours overrides -Days (more granular). -Days 1 default.
if ($Hours -gt 0) {
    $cutoff = (Get-Date).AddHours(-$Hours)
    $windowDesc = "$Hours hour(s)"
} else {
    $cutoff = (Get-Date).AddDays(-$Days)
    $windowDesc = "$Days day(s)"
}

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

$wrapperLog = Join-Path $ProjectRoot "logs\service_wrapper.log"
$mainLog    = Join-Path $ProjectRoot "logs\openclaw.log"

if (-not (Test-Path $wrapperLog)) {
    Write-Host "[ERROR] $wrapperLog not found" -ForegroundColor Red
    exit 1
}

$cutoffStr = $cutoff.ToString('yyyy-MM-dd HH:mm:ss')

# Read wrapper.log (UTF-8, share-read)
function Read-LogTail {
    param([string]$Path, [int]$MaxBytes = 524288)
    $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
                                  [System.IO.FileAccess]::Read,
                                  [System.IO.FileShare]::ReadWrite)
    try {
        $bytes = [Math]::Min($MaxBytes, $fs.Length)
        if ($bytes -lt $fs.Length) {
            $fs.Seek(-$bytes, [System.IO.SeekOrigin]::End) | Out-Null
        }
        $buf = New-Object byte[] $bytes
        [void]$fs.Read($buf, 0, $bytes)
        return [System.Text.Encoding]::UTF8.GetString($buf)
    } finally {
        $fs.Close()
    }
}

# Find exit events in wrapper.log
$wrapperText = Read-LogTail -Path $wrapperLog -MaxBytes 1048576
$wrapperLines = $wrapperText -split "`r?`n"

$crashes = @()
# Patterns are pure-ASCII to avoid PS 5.1 GBK-decoding-UTF8-file issues.
# Exit log format:    "...[wrapper] WARNING server.py XXXXX (code=N)"
# Cooldown log format: "...[wrapper] WARNING XXXXX 35s YYYYY 60s YYYYY"
#                      (two `(digits)s` groups distinguish from exit lines).
$exitPattern     = '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[^\[]+\[wrapper\][^\r\n]*?server\.py[^\r\n]*?code=(-?\d+)'
$cooldownPattern = '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[^\[]+\[wrapper\][^\r\n]*?WARNING[^\r\n]*?(\d+)\s*s[^\r\n]*?\d+\s*s'

foreach ($line in $wrapperLines) {
    if ($line -match $exitPattern) {
        $ts = $matches[1]
        $code = [int]$matches[2]
        try {
            $dt = [DateTime]::ParseExact($ts, 'yyyy-MM-dd HH:mm:ss', $null)
            if ($dt -gt $cutoff) {
                $crashes += @{ ts = $ts; exit_code = $code; reason = '' }
            }
        } catch { }
    } elseif ($line -match $cooldownPattern) {
        # Cooldown line refers to the most recent exit
        $ts = $matches[1]
        $secs = [int]$matches[2]
        try {
            $dt = [DateTime]::ParseExact($ts, 'yyyy-MM-dd HH:mm:ss', $null)
            if ($dt -gt $cutoff -and $crashes.Count -gt 0) {
                $crashes[-1].reason = "$secs s after restart (wrapper cooldown skipped restart)"
            }
        } catch { }
    }
}

# Take last $Last
$crashes = @($crashes | Select-Object -Last $Last)

if (-not $crashes -or $crashes.Count -eq 0) {
    if ($Json) {
        @{ crashes = @(); summary = "No server.py crashes in last $windowDesc" } | ConvertTo-Json -Depth 4
    } else {
        Write-Host "==========================================="
        Write-Host "  OpenClaw Crash Diagnosis"
        Write-Host "==========================================="
        Write-Host ""
        Write-Host "[OK] No server.py crashes in last $windowDesc" -ForegroundColor Green
    }
    exit 0
}

# For each crash, find ERROR/Exception lines in openclaw.log within +/- 60s
$mainText = if (Test-Path $mainLog) { Read-LogTail -Path $mainLog -MaxBytes 4194304 } else { '' }
$mainLines = $mainText -split "`r?`n"

foreach ($c in $crashes) {
    $crashTime = [DateTime]::ParseExact($c.ts, 'yyyy-MM-dd HH:mm:ss', $null)
    $window_start = $crashTime.AddSeconds(-60)
    $window_end   = $crashTime.AddSeconds(5)

    $contextLines = @()
    foreach ($ml in $mainLines) {
        if ($ml -match '"ts":\s*"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
            try {
                $mt = [DateTime]::ParseExact($matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
                if ($mt -ge $window_start -and $mt -le $window_end) {
                    if ($ml -match '"level":\s*"(ERROR|CRITICAL)"' -or $ml -match 'Traceback|Exception|FATAL') {
                        $contextLines += $ml
                    }
                }
            } catch { }
        }
    }
    # Dedupe by logger+msg (truncate)
    $top = $contextLines | Select-Object -Last 5 | ForEach-Object {
        if ($_.Length -gt 250) { $_.Substring(0, 250) + '...' } else { $_ }
    }
    $c.context = $top
}

# Output
if ($Json) {
    @{
        crashes = $crashes
        summary = "Found $($crashes.Count) crash(es) in last $windowDesc"
        cutoff = $cutoffStr
    } | ConvertTo-Json -Depth 4
} else {
    Write-Host "==========================================="
    Write-Host "  OpenClaw Crash Diagnosis"
    Write-Host "==========================================="
    Write-Host ""
    Write-Host ("Found {0} crash(es) in last {1} (since {2})" -f $crashes.Count, $windowDesc, $cutoffStr) -ForegroundColor Cyan
    Write-Host ""

    foreach ($c in $crashes) {
        Write-Host ("---  Crash @ {0}  exit_code={1}" -f $c.ts, $c.exit_code) -ForegroundColor Yellow
        if ($c.reason) { Write-Host ("     wrapper note: {0}" -f $c.reason) -ForegroundColor DarkYellow }
        if ($c.context -and $c.context.Count -gt 0) {
            Write-Host "     context (last 5 ERROR/exception lines within +/-60s):" -ForegroundColor DarkCyan
            foreach ($cl in $c.context) {
                Write-Host ("        $cl") -ForegroundColor DarkGray
            }
        } else {
            Write-Host "     [no ERROR/Exception lines in openclaw.log within +/-60s of crash]" -ForegroundColor DarkGray
        }
        Write-Host ""
    }
}

exit 0
