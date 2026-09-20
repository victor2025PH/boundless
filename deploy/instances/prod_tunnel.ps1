# prod_tunnel.ps1 - dedicated reverse tunnel for the PRODUCTION workspace (117 -> VPS).
#
# Publishes local zhiliao (127.0.0.1:18799) to VPS 127.0.0.1:18799 so VPS nginx can
# reverse-proxy it (https://katie.bd2026.cc). Deliberately a SEPARATE process/task from
# tenant_tunnel.ps1: tenant expose kicks the tenant tunnel to re-list ports (~10s blip
# each time), and the production entrance must NOT blip with tenant churn
# (2026-08-07 incident: the agents' entrance broke three times in one day from
# tenant-side ops; isolation is the fix, same doctrine as vision vs tenant tunnels).
#
# Same posture as tenant_tunnel.ps1: -N -C (compression: WAN tunnel measured ~30KB/s,
# HTML compresses ~8-10x), forwards land on VPS localhost only (never public), 10s
# reconnect loop, pid file for the watchdog to kick.
#
# Install (SYSTEM, survives reboot; run once, human decision):
#   schtasks /Create /TN ProdTunnel /TR "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File D:\boundless\deploy\instances\prod_tunnel.ps1" /SC ONSTART /RU SYSTEM /F
#   schtasks /Run /TN ProdTunnel
#
# ASCII-only on purpose (PowerShell 5.1 decodes BOM-less UTF-8 as GBK).
param(
    [int]$Port = 18799,
    [string]$Key = 'D:\chengjie-instances\.ops\vision_key',
    [string]$Vps = 'ubuntu@165.154.233.121'
)
$ErrorActionPreference = 'Continue'
$pidf = 'D:\chengjie-instances\.ops\prod_tunnel.pid'
$log  = 'D:\chengjie-instances\.ops\prod_tunnel.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Log($m) { ("{0} {1}" -f (Get-Date -Format s), $m) | Out-File $log -Append -Encoding utf8 }

# self-rotating log (>5MB keep tail)
try {
  if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
    Get-Content $log -Tail 2000 | Set-Content ($log + '.1') -Encoding utf8
    Remove-Item $log -Force
  }
} catch {}

# Singleton guard: two concurrent runners would fight over the remote port, and
# with stale-listener cleanup below each loser would kill the winner's healthy
# session (flap war). Task double-start must no-op instead.
$runnerMutex = New-Object System.Threading.Mutex($false, 'Global\ProdTunnelRunner')
$mutexOwned = $false
try { $mutexOwned = $runnerMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $mutexOwned = $true }
if (-not $mutexOwned) { Log 'duplicate runner detected (mutex busy); exiting'; exit 0 }

# 2026-08-12 zombie-listener self-heal: after a WAN blip the VPS sshd can keep
# holding the dead session's remote-forward listener, so every reconnect dies
# with "remote port forwarding failed" until the server culls it (observed
# 10-20 min outages). Detect that exact error and kill the stale holder on the
# VPS, then reconnect fast. Paired with VPS /etc/ssh/sshd_config.d/
# 60-tunnel-keepalive.conf (ClientAliveInterval 30 x3 = ~90s cull) as backstop.
$lastStaleCleanup = [datetime]::MinValue
function Clear-StaleForwards([string[]]$PortList) {
  $spec = (($PortList | ForEach-Object { "$_/tcp" }) -join ' ')
  Log ("bind failed; killing stale VPS listener(s): " + $spec)
  # -n (StdinNull) is mandatory on every ONE-SHOT ssh from this box (2026-09-11): the in-box
  # Windows ssh.exe 9.5p1 can hang forever after a fast remote command when stdin is an
  # unattended console (Win32-OpenSSH #1334). Here a hang would freeze this reconnect loop
  # with the tunnel down. The -N tunnel session itself is unaffected (no exec channel).
  & ssh '-n' '-o' 'BatchMode=yes' '-o' 'StrictHostKeyChecking=no' '-o' 'ConnectTimeout=15' `
      '-o' 'ServerAliveInterval=10' '-o' 'ServerAliveCountMax=2' `
      '-i' $Key $Vps ("sudo fuser -k " + $spec + " >/dev/null 2>&1; true") 2>$null | Out-Null
}

Log "prod tunnel runner start (port $Port)"
while ($true) {
  $sshArgs = @('-N', '-C', '-R', "127.0.0.1:${Port}:127.0.0.1:${Port}",
    '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3', '-o', 'StrictHostKeyChecking=no',
    '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-i', $Key, $Vps)
  Log "connect: port $Port"
  try {
    $p = Start-Process -FilePath 'ssh' -ArgumentList $sshArgs -NoNewWindow -PassThru `
         -RedirectStandardError ($log + '.ssh.err')
    $p.Id | Out-File $pidf -Encoding ascii -Force
    Wait-Process -Id $p.Id -ErrorAction SilentlyContinue
  } catch {
    Log ("exception: " + $_)
  }
  $sshErr = ''
  try { $sshErr = [IO.File]::ReadAllText($log + '.ssh.err') } catch {}
  if (($sshErr -match 'remote port forwarding failed') -and
      (((Get-Date) - $lastStaleCleanup).TotalSeconds -gt 60)) {
    $lastStaleCleanup = Get-Date
    Clear-StaleForwards @($Port)
    Log 'tunnel dropped (stale bind); cleanup done, reconnect in 3s'
    Start-Sleep -Seconds 3
    continue
  }
  Log 'tunnel dropped/killed; reconnect in 10s'
  Start-Sleep -Seconds 10
}
