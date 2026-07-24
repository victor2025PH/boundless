# stop_instance.ps1 — stop one chengjie dual-instance (port-holder must be engine main.py)
# Usage: powershell -ExecutionPolicy Bypass -File .\stop_instance.ps1 -Instance tongyi [-TimeoutSec 20] [-GraceSec 8]
#
# Phase3 graceful stop:
#   1) taskkill /T (no /F) — gives python a chance to run atexit / clear sentinel
#   2) wait up to GraceSec for ports to release
#   3) if still listening → taskkill /T /F (hard kill, then clear sentinel ourselves)
#
# ASCII-lean log lines mixed with existing Chinese status (file already bilingual).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('tongyi', 'zhiliao')]
    [string]$Instance,
    [int]$TimeoutSec = 20,
    [int]$GraceSec = 8
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$PortMap = @{ tongyi = @(18899, 18887); zhiliao = @(18799, 18787) }
$Ports   = $PortMap[$Instance]

function Fail([string]$msg) {
    Write-Host "[stop-$Instance] 错误: $msg" -ForegroundColor Red
    exit 1
}

function Get-ListeningPorts {
    $still = @()
    foreach ($port in $Ports) {
        if (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count) {
            $still += $port
        }
    }
    return $still
}

# Find port holders (primary + alt)
$holders = @{}
foreach ($port in $Ports) {
    foreach ($c in @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
        $holderPid = [int]$c.OwningProcess
        if (-not $holders.ContainsKey($holderPid)) { $holders[$holderPid] = @() }
        if ($holders[$holderPid] -notcontains $port) { $holders[$holderPid] += $port }
    }
}
if (-not $holders.Count) {
    Write-Host "[stop-$Instance] 端口 $($Ports -join '/') 无监听——实例未在跑，幂等退出" -ForegroundColor Green
    exit 0
}

$targets = @()
foreach ($holderPid in $holders.Keys) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
    if (-not $p) { continue }
    if ($p.CommandLine -like '*main.py*') {
        $targets += [pscustomobject]@{ ProcessId = $holderPid; Name = $p.Name; Ports = $holders[$holderPid] }
    } else {
        Fail "端口 $($holders[$holderPid] -join ',') 持有者 PID=$holderPid($($p.Name)) 命令行不含 main.py，不是本引擎实例。拒绝停止（人工核实：Get-CimInstance Win32_Process -Filter `"ProcessId=$holderPid`" | Select CommandLine）"
    }
}
if (-not $targets.Count) {
    Write-Host "[stop-$Instance] 持有进程已自行退出，幂等退出" -ForegroundColor Green
    exit 0
}

$dataRoots = New-Object 'System.Collections.Generic.HashSet[string]'
foreach ($t in $targets) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($t.ProcessId)" -ErrorAction SilentlyContinue
    for ($hop = 0; ($hop -lt 2) -and $p; $hop++) {
        if ($p.CommandLine -match 'AITR_DATA_DIR=([^"&]+)') {
            $root = $Matches[1].Trim().TrimEnd('\')
            if ($root) { [void]$dataRoots.Add($root) }
        }
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
    }
}

# 1) Soft kill first (no /F) — atexit may clear sentinel.
# Soft taskkill often fails without elevation (Access denied). Run via cmd so
# stderr does not become a terminating NativeCommandError under $EAP=Stop.
#
# Phase10 SLA fix: headless python (no window) can't receive WM_CLOSE →
# taskkill without /F returns "can only be terminated forcefully". Waiting the
# full grace for a soft-kill that was REJECTED is pure wasted downtime (~8s every
# restart). So: only wait grace when at least one soft-kill was ACCEPTED; if all
# were rejected, skip straight to hard kill.
$softAccepted = $false
foreach ($t in $targets) {
    Write-Host "[stop-$Instance] soft-stop PID=$($t.ProcessId)($($t.Name)) ports=$($t.Ports -join ',') (taskkill /T)"
    $softOut = & cmd /c "taskkill /PID $($t.ProcessId) /T 2>&1"
    $accepted = $false
    foreach ($line in @($softOut)) {
        Write-Host "[stop-$Instance]   $line" -ForegroundColor DarkGray
        if ($line -match 'SUCCESS') { $accepted = $true }
    }
    if ($accepted) { $softAccepted = $true }
}

$released = $false
if ($softAccepted) {
    Write-Host "[stop-$Instance] soft-kill accepted; waiting up to ${GraceSec}s for atexit + port release" -ForegroundColor DarkGray
    $graceDeadline = (Get-Date).AddSeconds([Math]::Max(0, $GraceSec))
    while ((Get-Date) -lt $graceDeadline) {
        Start-Sleep -Milliseconds 400
        if (-not (Get-ListeningPorts).Count) { $released = $true; break }
    }
} else {
    # All soft-kills rejected (headless process) — do not burn the grace window.
    Write-Host "[stop-$Instance] soft-kill not applicable (headless); skipping grace, hard-stop now" -ForegroundColor DarkGray
    Start-Sleep -Milliseconds 200
    if (-not (Get-ListeningPorts).Count) { $released = $true }
}

# 2) Hard kill if still up
if (-not $released) {
    foreach ($t in $targets) {
        $alive = Get-Process -Id $t.ProcessId -ErrorAction SilentlyContinue
        if (-not $alive) { continue }
        Write-Host "[stop-$Instance] hard-stop PID=$($t.ProcessId) (taskkill /T /F)" -ForegroundColor Yellow
        try {
            $hardOut = & cmd /c "taskkill /PID $($t.ProcessId) /T /F" 2>&1
            foreach ($line in @($hardOut)) {
                Write-Host "[stop-$Instance]   $line" -ForegroundColor DarkGray
            }
        } catch {
            Write-Host "[stop-$Instance] hard-stop error: $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    # Hard kill skips atexit → clear sentinel ourselves
    foreach ($root in $dataRoots) {
        $sentinel = Join-Path $root 'logs\run_sentinel.json'
        if (Test-Path $sentinel) {
            Remove-Item $sentinel -Force -ErrorAction SilentlyContinue
            Write-Host "[stop-$Instance] cleared sentinel after hard-stop: $sentinel" -ForegroundColor DarkGray
        }
    }
} else {
    Write-Host "[stop-$Instance] soft-stop released ports within grace" -ForegroundColor DarkGray
    # Soft path usually clears sentinel via atexit; best-effort sweep if leftover
    foreach ($root in $dataRoots) {
        $sentinel = Join-Path $root 'logs\run_sentinel.json'
        if (Test-Path $sentinel) {
            Remove-Item $sentinel -Force -ErrorAction SilentlyContinue
            Write-Host "[stop-$Instance] cleared leftover sentinel: $sentinel" -ForegroundColor DarkGray
        }
    }
}

# 3) Confirm ports free
$deadline = (Get-Date).AddSeconds($TimeoutSec)
do {
    Start-Sleep -Milliseconds 500
    $still = Get-ListeningPorts
    if (-not $still.Count) {
        Write-Host "[stop-$Instance] done — 端口 $($Ports -join '/') 已全部释放" -ForegroundColor Green
        exit 0
    }
} while ((Get-Date) -lt $deadline)

Fail "等待 ${TimeoutSec}s 后端口 $($still -join ',') 仍在监听，请人工排查（netstat -ano | findstr $($still[0])）"
