param(
    [Parameter(Mandatory = $true)]
    [string]$HostIp,

    [Parameter(Mandatory = $true)]
    [string]$HostId,

    [Parameter(Mandatory = $true)]
    [string]$HostName,

    [string]$SshUser = "administrator",
    [string]$ProjectDir = "C:\openclaw\mobile-auto-project",
    [string]$CoordinatorUrl = "http://192.168.0.117:18080",
    [int]$WorkerPort = 8000,
    [string]$PythonPath = "",
    [switch]$InstallRequirements,
    [switch]$SkipStart
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PackageName = "openclaw-fresh-worker-$HostId.zip"
$StageRoot = Join-Path $env:TEMP ("openclaw-fresh-worker-stage-" + [guid]::NewGuid().ToString("N"))
$PackagePath = Join-Path $env:TEMP $PackageName
$RemotePackage = "C:\Windows\Temp\$PackageName"

function Test-ExcludedDir {
    param([string]$RelativePath)
    $parts = $RelativePath -split '[\\/]'
    $excluded = @(
        ".git", ".pytest_cache", "__pycache__", ".claude", ".cursor", ".idea", ".vscode",
        "data", "logs", "temp", "reports", "debug", "apk_repo",
        "node_modules", "venv", ".venv", "env", "dist", "build"
    )
    foreach ($p in $parts) {
        if ($excluded -contains $p) { return $true }
    }
    return $false
}

function Test-ExcludedFile {
    param([string]$RelativePath)
    $name = Split-Path $RelativePath -Leaf
    $ext = [IO.Path]::GetExtension($name).ToLowerInvariant()
    $runtimeFiles = @(
        "config\analytics_history.json",
        "config\central_push_queue.db",
        "config\central_push_queue.db-shm",
        "config\central_push_queue.db-wal",
        "config\cluster_locks.db",
        "config\cluster_state.json",
        "config\device_aliases.json",
        "config\device_registry.json",
        "config\mock_location_cache.json",
        "config\routers.json"
    )
    $normalized = $RelativePath.Replace("/", "\")
    if ($runtimeFiles -contains $normalized) { return $true }
    if ($name -in @(".env", ".env.local", ".restart-required", "task_detail.json", "tasks_recent.json")) { return $true }
    if ($ext -in @(".pyc", ".pyo", ".db", ".log", ".sqlite", ".sqlite3", ".apk", ".ipa", ".zip", ".tmp", ".pid", ".bak", ".orig")) {
        return $true
    }
    if (($RelativePath -notmatch '[\\/]') -and ($ext -in @(".png", ".jpg", ".jpeg", ".xml"))) {
        return $true
    }
    return $false
}

function New-FreshPackage {
    if (Test-Path $StageRoot) { Remove-Item -LiteralPath $StageRoot -Recurse -Force }
    if (Test-Path $PackagePath) { Remove-Item -LiteralPath $PackagePath -Force }
    New-Item -ItemType Directory -Path $StageRoot -Force | Out-Null

    Get-ChildItem -LiteralPath $RepoRoot -Force | ForEach-Object {
        $rel = $_.Name
        if ($_.PSIsContainer) {
            if (-not (Test-ExcludedDir $rel)) {
                Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $StageRoot $_.Name) -Recurse -Force
            }
        } else {
            if (-not (Test-ExcludedFile $rel)) {
                Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $StageRoot $_.Name) -Force
            }
        }
    }

    Get-ChildItem -LiteralPath $StageRoot -Recurse -Force | Sort-Object FullName -Descending | ForEach-Object {
        $rel = $_.FullName.Substring($StageRoot.Length).TrimStart("\", "/")
        if ($_.PSIsContainer) {
            if (Test-ExcludedDir $rel) {
                Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            }
        } else {
            if ((Test-ExcludedDir (Split-Path $rel -Parent)) -or (Test-ExcludedFile $rel)) {
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            }
        }
    }

    Compress-Archive -Path (Join-Path $StageRoot "*") -DestinationPath $PackagePath -CompressionLevel Optimal -Force
    Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
}

function Invoke-RemoteFreshDeploy {
    $installReq = if ($InstallRequirements) { '$true' } else { '$false' }
    $skipStart = if ($SkipStart) { '$true' } else { '$false' }
    $pyLiteral = $PythonPath.Replace("'", "''")
    $projectLiteral = $ProjectDir.Replace("'", "''")
    $coordLiteral = $CoordinatorUrl.Replace("'", "''")
    $hostIdLiteral = $HostId.Replace("'", "''")
    $hostNameLiteral = $HostName.Replace("'", "''")
    $pkgLiteral = $RemotePackage.Replace("'", "''")

    $remoteScript = @"
`$ErrorActionPreference = 'Stop'
`$ProgressPreference = 'SilentlyContinue'
`$Project = '$projectLiteral'
`$Package = '$pkgLiteral'
`$CoordinatorUrl = '$coordLiteral'
`$WorkerPort = $WorkerPort
`$HostId = '$hostIdLiteral'
`$HostName = '$hostNameLiteral'
`$PythonPath = '$pyLiteral'
`$InstallRequirements = $installReq
`$SkipStart = $skipStart

function Find-Python {
    param([string]`$Preferred)
    if (`$Preferred -and (Test-Path `$Preferred)) { return `$Preferred }
    `$candidates = @(
        'C:\Program Files\Python313\python.exe',
        'C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe',
        'C:\Python313\python.exe',
        'C:\Program Files\Python312\python.exe',
        'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe',
        'C:\Python312\python.exe'
    )
    foreach (`$p in `$candidates) {
        if (Test-Path `$p) { return `$p }
    }
    `$where = (where.exe python 2>`$null) | Where-Object { `$_ -and (`$_ -notmatch 'WindowsApps') }
    if (`$where) { return @(`$where)[0] }
    throw 'Python not found'
}

if (`$Project -notmatch '^[A-Za-z]:\\openclaw\\mobile-auto-project$') {
    throw "Refuse to clean unexpected project path: `$Project"
}
if (-not (Test-Path `$Package)) {
    throw "Package not found: `$Package"
}

`$Python = Find-Python `$PythonPath
`$parent = Split-Path -Parent `$Project
New-Item -ItemType Directory -Path `$parent -Force | Out-Null
Set-Location 'C:\Windows\Temp'

try {
    Get-ScheduledTask | Where-Object { `$_.TaskName -like 'OpenClaw*' } | ForEach-Object {
        try { Stop-ScheduledTask -TaskName `$_.TaskName -ErrorAction SilentlyContinue } catch {}
        try { Unregister-ScheduledTask -TaskName `$_.TaskName -Confirm:`$false -ErrorAction SilentlyContinue } catch {}
    }
} catch {}

`$procRegex = 'mobile-auto-project|service_wrapper\.py|\bserver\.py|uvicorn\s+.*src\.host\.api'
`$old = Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -match `$procRegex }
foreach (`$p in `$old) {
    try { Stop-Process -Id `$p.ProcessId -Force -ErrorAction Stop } catch {}
}
Start-Sleep -Seconds 2

New-Item -ItemType Directory -Path `$Project -Force | Out-Null
`$cleaned = `$false
for (`$i = 0; `$i -lt 3 -and -not `$cleaned; `$i++) {
    try {
        Get-ChildItem -LiteralPath `$Project -Force -ErrorAction Stop | Remove-Item -Recurse -Force -ErrorAction Stop
        `$cleaned = `$true
    } catch {
        Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -match `$procRegex } | ForEach-Object {
            try { Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
        }
        Start-Sleep -Seconds 2
    }
}
if (-not `$cleaned) {
    throw "Failed to clean project contents: `$Project"
}
Expand-Archive -LiteralPath `$Package -DestinationPath `$Project -Force

New-Item -ItemType Directory -Path (Join-Path `$Project 'config') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path `$Project 'data') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path `$Project 'logs') -Force | Out-Null

`$clusterYaml = @(
    '# OpenClaw 集群配置',
    'role: worker',
    ('coordinator_url: "' + `$CoordinatorUrl + '"'),
    ('local_port: ' + `$WorkerPort),
    'shared_secret: "CHANGE_ME"',
    'heartbeat_interval: 10',
    'host_timeout: 30',
    'auto_join: true',
    ('host_name: "' + `$HostName + '"'),
    ('host_id: "' + `$HostId + '"'),
    'advertise_ip: ""',
    'reverse_probe_interval: 30',
    'reverse_probe_max_interval: 300',
    'reverse_probe_backoff_multiplier: 2.0',
    'reverse_probe_startup_delay: 10'
) -join [Environment]::NewLine
Set-Content -LiteralPath (Join-Path `$Project 'config\cluster.yaml') -Value `$clusterYaml -Encoding UTF8

`$launchEnv = @(
    '# Fresh worker launch profile',
    ('OPENCLAW_PORT=' + `$WorkerPort),
    'OPENCLAW_HOST=0.0.0.0'
) -join [Environment]::NewLine
Set-Content -LiteralPath (Join-Path `$Project 'config\launch.env') -Value `$launchEnv -Encoding UTF8
Set-Content -LiteralPath (Join-Path `$Project 'config\device_aliases.json') -Value '{}' -Encoding UTF8
Set-Content -LiteralPath (Join-Path `$Project 'config\device_registry.json') -Value '{}' -Encoding UTF8
Set-Content -LiteralPath (Join-Path `$Project 'config\cluster_state.json') -Value '{}' -Encoding UTF8

Get-ChildItem -LiteralPath (Join-Path `$Project 'config') -Filter '*.db*' -Force -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path `$Project '.restart-required') -Force -ErrorAction SilentlyContinue

[Environment]::SetEnvironmentVariable('OPENCLAW_PORT', [string]`$WorkerPort, 'Machine')
[Environment]::SetEnvironmentVariable('OPENCLAW_PORT', [string]`$WorkerPort, 'User')
[Environment]::SetEnvironmentVariable('OPENCLAW_HOST', '0.0.0.0', 'Machine')
[Environment]::SetEnvironmentVariable('OPENCLAW_HOST', '0.0.0.0', 'User')
`$env:OPENCLAW_PORT = [string]`$WorkerPort
`$env:OPENCLAW_HOST = '0.0.0.0'

if (`$InstallRequirements) {
    & `$Python -m pip install -r (Join-Path `$Project 'requirements.txt')
}

try { Unregister-ScheduledTask -TaskName 'OpenClaw-Worker' -Confirm:`$false -ErrorAction SilentlyContinue } catch {}
`$taskArgs = '"' + (Join-Path `$Project 'service_wrapper.py') + '" --no-auto-update --health-interval 60 --max-restarts 20'
`$taskAction = New-ScheduledTaskAction -Execute `$Python -Argument `$taskArgs -WorkingDirectory `$Project
`$taskTrigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'OpenClaw-Worker' -Action `$taskAction -Trigger `$taskTrigger -RunLevel Highest -Force | Out-Null

`$startInfo = `$null
if (-not `$SkipStart) {
    `$cmd = '"' + `$Python + '" service_wrapper.py --no-auto-update --health-interval 60 --max-restarts 20'
    `$startInfo = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = `$cmd; CurrentDirectory = `$Project }
    Start-Sleep -Seconds 10
}

`$health = `$null
try {
    `$health = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health" -f `$WorkerPort) -TimeoutSec 10
} catch {
    `$health = @{ status = 'probe_failed'; error = `$_.Exception.Message }
}
`$procs = Get-CimInstance Win32_Process | Where-Object { `$_.CommandLine -match `$procRegex } | Select-Object ProcessId,CommandLine
`$adb = ''
try { `$adb = (adb devices | Out-String) } catch { `$adb = `$_.Exception.Message }

[pscustomobject]@{
    ok = `$true
    computer = `$env:COMPUTERNAME
    project = `$Project
    python = `$Python
    host_id = `$HostId
    host_name = `$HostName
    coordinator_url = `$CoordinatorUrl
    worker_port = `$WorkerPort
    killed = @(`$old).Count
    start_result = if (`$startInfo) { `$startInfo.ReturnValue } else { `$null }
    start_pid = if (`$startInfo) { `$startInfo.ProcessId } else { `$null }
    health = `$health
    processes = `$procs
    adb = `$adb
} | ConvertTo-Json -Depth 8 -Compress
"@

    $RemoteScriptPath = "C:\Windows\Temp\openclaw-fresh-deploy-$HostId.ps1"
    $LocalRemoteScriptPath = Join-Path $env:TEMP "openclaw-fresh-deploy-$HostId.ps1"
    Set-Content -LiteralPath $LocalRemoteScriptPath -Value $remoteScript -Encoding UTF8
    scp -o BatchMode=yes -o ConnectTimeout=10 $LocalRemoteScriptPath "$SshUser@${HostIp}:$RemoteScriptPath"
    try {
        ssh -o BatchMode=yes -o ConnectTimeout=10 "$SshUser@$HostIp" "cd /d C:\Windows\Temp && powershell -NoProfile -ExecutionPolicy Bypass -File $RemoteScriptPath"
    } finally {
        Remove-Item -LiteralPath $LocalRemoteScriptPath -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "[fresh-deploy] building package from $RepoRoot" -ForegroundColor Cyan
New-FreshPackage
$sizeMb = [Math]::Round((Get-Item $PackagePath).Length / 1MB, 2)
Write-Host "[fresh-deploy] package: $PackagePath ($sizeMb MB)" -ForegroundColor Cyan

Write-Host "[fresh-deploy] upload to $SshUser@${HostIp}:$RemotePackage" -ForegroundColor Cyan
scp -o BatchMode=yes -o ConnectTimeout=10 $PackagePath "$SshUser@${HostIp}:$RemotePackage"

Write-Host "[fresh-deploy] remote clean install: $HostName ($HostId)" -ForegroundColor Cyan
Invoke-RemoteFreshDeploy

Remove-Item -LiteralPath $PackagePath -Force -ErrorAction SilentlyContinue
