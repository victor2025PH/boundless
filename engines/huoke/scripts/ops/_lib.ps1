# Shared helpers for OpenClaw ops scripts
# Dot-source via:  . "$PSScriptRoot\_lib.ps1"

# Match patterns for OpenClaw python processes
# Three known launch shapes:
#   1. service_wrapper.py        (recommended, with auto-restart + OTA)
#   2. server.py                 (dev mode, no wrapper)
#   3. uvicorn src.host.api:app  (raw uvicorn, no wrapper, no log redirect)
$script:OPENCLAW_PROC_REGEX = 'service_wrapper\.py|\bserver\.py|uvicorn\s+(?:-{1,2}\S+\s+\S+\s+)*src\.host\.api'

function Get-OpenClawProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match $script:OPENCLAW_PROC_REGEX }
}

function Get-OpenClawLaunchTag {
    param([string]$cmdLine)
    if ($cmdLine -match 'service_wrapper\.py') { return 'wrapper' }
    if ($cmdLine -match '\bserver\.py')        { return 'server ' }
    if ($cmdLine -match 'uvicorn.*src\.host\.api') { return 'uvicorn' }
    return 'python '
}

function Get-OpenClawProcessKind {
    # Returns one of: 'wrapper', 'server', 'uvicorn', or $null if no process
    $procs = Get-OpenClawProcesses
    if (-not $procs) { return $null }
    foreach ($p in $procs) {
        if ($p.CommandLine -match 'service_wrapper\.py') { return 'wrapper' }
    }
    foreach ($p in $procs) {
        if ($p.CommandLine -match '\bserver\.py') { return 'server' }
    }
    return 'uvicorn'
}
