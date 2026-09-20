# ensure_lan_portproxy.ps1 — ChatX LAN entrance self-heal (portproxy + firewall)
#
# Production web binds 127.0.0.1:18799. Phone QR / seat LAN go through
# netsh portproxy: 192.168.x.x:18799 -> 127.0.0.1:18799. The rule can stay
# in the registry while the listen socket dies after DHCP / iphlpsvc jitter
# (localhost curl works, phone scan of 192.168.0.117 is connection refused).
# Idempotent: add rule, restart iphlpsvc if not listening, ensure firewall,
# TCP probe.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy\instances\ensure_lan_portproxy.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\instances\ensure_lan_portproxy.ps1 -Port 18799 -ListenAddr 192.168.0.117
#
# Exit: 0 = LAN reachable; 1 = still down after repair.

[CmdletBinding()]
param(
    [int]$Port = 18799,
    [string]$ListenAddr = '',
    [string]$ConnectAddr = '127.0.0.1'
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

function Say([string]$msg, [string]$color = 'Gray') {
    Write-Host "[lan-proxy] $msg" -ForegroundColor $color
}

function Get-LanIPv4 {
    $nics = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike '127.*' -and
            $_.IPAddress -notlike '169.254.*' -and
            $_.PrefixOrigin -ne 'WellKnown' -and
            $_.InterfaceAlias -notmatch 'ZeroTier|Tailscale|vEthernet|WSL|VMware|VirtualBox|Bluetooth|蓝牙'
        })
    $eth = @($nics | Where-Object { $_.InterfaceAlias -match '以太网|Ethernet' -and $_.IPAddress -like '192.168.*' })
    if ($eth.Count) { return [string]$eth[0].IPAddress }
    $lan = @($nics | Where-Object { $_.IPAddress -like '192.168.*' })
    if ($lan.Count) { return [string]$lan[0].IPAddress }
    if ($nics.Count) { return [string]$nics[0].IPAddress }
    return ''
}

function Test-Tcp([string]$ip, [int]$p, [int]$ms = 800) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect($ip, $p, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne($ms, $false)
        if (-not $ok) { try { $c.Close() } catch {}; return $false }
        $c.EndConnect($iar)
        $c.Close()
        return $true
    } catch { return $false }
}

function Get-ProxyListen([int]$p) {
    $out = @()
    foreach ($ln in @(netsh interface portproxy show v4tov4 2>$null)) {
        if ("$ln" -match '^\s*(\d+\.\d+\.\d+\.\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s*$' -and
            [int]$Matches[2] -eq $p) {
            $out += [pscustomobject]@{
                ListenAddr = $Matches[1]; ListenPort = [int]$Matches[2]
                ConnectAddr = $Matches[3]; ConnectPort = [int]$Matches[4]
            }
        }
    }
    return $out
}

$listen = if ($ListenAddr) { $ListenAddr.Trim() } else { Get-LanIPv4 }
if (-not $listen) {
    Say "no LAN IPv4; cannot create portproxy" 'Red'
    exit 1
}

Say ("target={0}:{1} -> {2}:{1}" -f $listen, $Port, $ConnectAddr)

$rules = @(Get-ProxyListen $Port | Where-Object { $_.ListenAddr -eq $listen })
if (-not $rules.Count) {
    Say "adding portproxy rule"
    netsh interface portproxy delete v4tov4 listenaddress=$listen listenport=$Port 2>$null | Out-Null
    $add = netsh interface portproxy add v4tov4 listenaddress=$listen listenport=$Port connectaddress=$ConnectAddr connectport=$Port
    if ($LASTEXITCODE -ne 0 -and "$add") { Say "add: $add" 'Yellow' }
}

$listening = @(Get-NetTCPConnection -LocalAddress $listen -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if (-not $listening.Count) {
    Say "rule present but not listening — restart iphlpsvc"
    try { Restart-Service iphlpsvc -Force -ErrorAction Stop } catch {
        Say ("restart iphlpsvc failed: {0}" -f $_.Exception.Message) 'Yellow'
    }
    Start-Sleep -Seconds 2
    netsh interface portproxy delete v4tov4 listenaddress=$listen listenport=$Port 2>$null | Out-Null
    netsh interface portproxy add v4tov4 listenaddress=$listen listenport=$Port connectaddress=$ConnectAddr connectport=$Port | Out-Null
    Start-Sleep -Seconds 1
}

$fwName = "ChatX workspace LAN $Port"
$fw = @(Get-NetFirewallRule -DisplayName $fwName -ErrorAction SilentlyContinue)
if (-not $fw.Count) {
    Say ("add inbound firewall {0} (192.168.0.0/24 only)" -f $fwName)
    try {
        New-NetFirewallRule -DisplayName $fwName -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port -RemoteAddress 192.168.0.0/24 `
            -Profile Any -ErrorAction Stop | Out-Null
    } catch {
        Say ("firewall rule write failed: {0}" -f $_.Exception.Message) 'Yellow'
    }
}

$ok = Test-Tcp $listen $Port
$sock = @(Get-NetTCPConnection -LocalAddress $listen -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($ok -and $sock.Count) {
    Say ("LAN OK  http://{0}:{1}  listen=yes" -f $listen, $Port) 'Green'
    exit 0
}
Say ("LAN still down  http://{0}:{1} listen={2} tcp={3}" -f $listen, $Port, $sock.Count, $ok) 'Red'
exit 1
