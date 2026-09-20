# net_probe_117.ps1 — 117 网络监控探针 v2（2026-09-03 重写；v1 见 git 历史）
# 计划任务 Boundless-net-probe-117 每 1 分钟一拍，输出两处（目录 D:\chengjie-instances\.ops）：
#   net_probe_117.log         每拍一行状态快照（有线/无线/默认路由/三段 ping/176 业务口）
#   net_probe_117_events.log  只记「变化」：有线链路起落或速率变化、网关/公网通断、掉电重启(Kernel-Power 41)
# 判读口径：
#   wired=Disconnected/0bps  = 有线口无载波（网线/对端口/网卡 PHY 三者之一）
#   wired=Up 但速率 100 Mbps = 千兆链路劣化（线芯/接触/PHY 边缘状态）
#   gw ok 而 pub 0           = 局域网通、出口断 → 路由器 WAN/光猫/运营商
#   gw 0 且 wifi 仍 connected = 路由器本体失联
#   POWER-LOSS               = 上一轮到本轮之间发生过非正常关机（掉电/死机/长按电源强制重启），不是网络故障本身
#   SELF-HEAL                = 无线「假在线」（connected、信号满格、但网关连续 ≥2 拍不通）→ 自动重启无线网卡
#                              2026-09-04 实录 12:09-12:16 / 12:44-12:48 两次，此前只能整机强制重启
# v1 的 ping 列在计划任务下恒 0/5：文件无 BOM 被 PS5.1 按 GBK 读，中文正则失配。v2 改用 .NET Ping，与语言无关。
# 本文件必须以 UTF-8 with BOM 保存（计划任务用的是 powershell 5.1）。
$ErrorActionPreference = 'SilentlyContinue'
$dir   = 'D:\chengjie-instances\.ops'
$log   = Join-Path $dir 'net_probe_117.log'
$evlog = Join-Path $dir 'net_probe_117_events.log'
$stateFile = Join-Path $dir 'net_probe_117.state.json'
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
foreach ($f in @($log, $evlog)) {
  if ((Test-Path $f) -and ((Get-Item $f).Length -gt 5MB)) { Move-Item $f "$f.1" -Force }
}
$now = Get-Date
$ts  = $now.ToString('yyyy-MM-dd HH:mm:ss')

# ---- 上一轮状态 ----
$prev = $null
if (Test-Path $stateFile) { try { $prev = Get-Content $stateFile -Raw | ConvertFrom-Json } catch { $prev = $null } }

# ---- 有线口（板载 Realtek GbE；按描述选，不依赖「以太网」这个会随重装变化的名字）----
$wired = Get-NetAdapter -Physical | Where-Object { $_.InterfaceDescription -match 'Realtek.*GbE|Realtek PCIe GbE' } | Select-Object -First 1
if ($wired) {
  $wst = Get-NetAdapterStatistics -Name $wired.Name
  $wiredState = "{0}/{1}" -f $wired.Status, ($wired.LinkSpeed -replace '\s+', '')
  $wiredStr = "wired={0} rxErr={1} rxB={2}" -f $wiredState, $wst.ReceivedPacketErrors, $wst.ReceivedBytes
} else {
  $wiredState = 'ABSENT'
  $wiredStr = 'wired=ABSENT(设备不在列——PCIe 掉卡或被禁用)'
}

# ---- 无线（USB 棒子）----
$wifi = Get-NetAdapter -Physical | Where-Object { $_.InterfaceDescription -match 'WiFi|Wireless|WLAN|802\.11' -and $_.Status -eq 'Up' } | Select-Object -First 1
$ssid = '-'; $sig = '-'
if ($wifi) {
  $wl = netsh wlan show interfaces | Out-String
  if ($wl -match 'SSID\s*:\s*(\S+)') { $ssid = $Matches[1] }
  if ($wl -match '(\d{1,3})%') { $sig = $Matches[1] + '%' }
  $wifiStr = "wifi={0}@{1}/{2} sig={3}" -f $wifi.Status, $ssid, ($wifi.LinkSpeed -replace '\s+', ''), $sig
} else {
  $wifiStr = 'wifi=down'
}

# ---- 默认路由：取 (RouteMetric+InterfaceMetric) 最小的那条，避开 ZeroTier 的 9999 兜底路由 ----
$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' |
  Sort-Object { $_.RouteMetric + $_.InterfaceMetric } | Select-Object -First 1
$gw = $null; $routeStr = 'route=none'
if ($route) { $gw = $route.NextHop; $routeStr = "route={0}via{1}" -f $route.InterfaceAlias, $gw }

# ---- ping（.NET，不依赖语言）----
function Probe-Ping([string]$ip, [int]$n = 3) {
  if (-not $ip) { return @{ ok = 0; avg = -1 } }
  $p = New-Object System.Net.NetworkInformation.Ping
  $ok = 0; $sum = 0
  for ($i = 0; $i -lt $n; $i++) {
    try { $r = $p.Send($ip, 1000); if ($r.Status -eq 'Success') { $ok++; $sum += $r.RoundtripTime } } catch {}
  }
  $avg = -1; if ($ok -gt 0) { $avg = [int]($sum / $ok) }
  return @{ ok = $ok; avg = $avg }
}
$pgw  = Probe-Ping $gw
$ppub = Probe-Ping '223.5.5.5'
$pvps = Probe-Ping '165.154.233.121'

# ---- 176 业务口（TCP，176 不回 ICMP 属特性）----
$tcpOk = $false
try {
  $c = New-Object Net.Sockets.TcpClient
  $ar = $c.BeginConnect('192.168.0.176', 11434, $null, $null)
  $tcpOk = $ar.AsyncWaitHandle.WaitOne(2000) -and $c.Connected
  $c.Close()
} catch { $tcpOk = $false }

# ---- 掉电/死机重启检测：上一轮之后有没有 Kernel-Power 41 ----
$powerLoss = @()
$sinceT = $now.AddMinutes(-3)
if ($prev -and $prev.ts) { try { $sinceT = [datetime]$prev.ts } catch {} }
$kp = Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 41; StartTime = $sinceT } -ErrorAction SilentlyContinue
foreach ($e in $kp) { $powerLoss += $e.TimeCreated.ToString('MM-dd HH:mm:ss') }

# ---- 写快照 ----
$line = "[{0}] {1}  {2}  {3}  gw={4}/3({5}ms) pub={6}/3({7}ms) vps={8}/3({9}ms) tcp176={10}" -f `
  $ts, $wiredStr, $wifiStr, $routeStr, $pgw.ok, $pgw.avg, $ppub.ok, $ppub.avg, $pvps.ok, $pvps.avg, $(if ($tcpOk) { 'ok' } else { 'FAIL' })
if ($powerLoss.Count -gt 0) { $line += "  POWER-LOSS-REBOOT@" + ($powerLoss -join ',') }
# 显式 UTF8：PS5.1 的 Add-Content 默认 ANSI(GBK)，pwsh7/编辑器按 UTF-8 读会变乱码
Add-Content -Path $log -Value $line -Encoding UTF8

# ---- 变化才写 events ----
$cur = [ordered]@{
  ts = $ts; wired = $wiredState; ssid = $ssid; wifiUp = [bool]$wifi
  gwUp = ($pgw.ok -gt 0); pubUp = ($ppub.ok -gt 0); tcp176 = $tcpOk; gw = $gw
}
$events = @()

# ---- 无线假在线自愈：wifi 仍 Up、默认路由在局域网网关上、但网关与公网连续两拍全 0 ----
# 有线口在的话不动（真出口是有线）；只重启无线适配器（~5s 重连），不碰整机。10 分钟内最多一次防抖。
$zombieN = 0; if ($prev -and $prev.zombieN) { $zombieN = [int]$prev.zombieN }
$lastHeal = $null; if ($prev -and $prev.lastHeal) { try { $lastHeal = [datetime]$prev.lastHeal } catch {} }
$wiredCarrier = ($wired -and $wired.Status -eq 'Up')
$zombie = ($wifi -and -not $wiredCarrier -and $gw -match '^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)' -and $pgw.ok -eq 0 -and $ppub.ok -eq 0)
if ($zombie) { $zombieN++ } else { $zombieN = 0 }
if ($zombieN -ge 2 -and (-not $lastHeal -or ($now - $lastHeal).TotalMinutes -ge 10)) {
  $wifiName = $wifi.Name
  try {
    Restart-NetAdapter -Name $wifiName -Confirm:$false -ErrorAction Stop
    $events += "SELF-HEAL: wifi '{0}' 假在线（connected 但网关/公网连续 {1} 拍不通）→ 已 Restart-NetAdapter" -f $wifiName, $zombieN
  } catch {
    $events += "SELF-HEAL-FAILED: Restart-NetAdapter '{0}' 失败：{1}" -f $wifiName, $_.Exception.Message
  }
  $lastHeal = $now; $zombieN = 0
}
$cur.zombieN = $zombieN
if ($lastHeal) { $cur.lastHeal = $lastHeal.ToString('yyyy-MM-dd HH:mm:ss') }

# ---- ZeroTier 兜底默认路由自愈（2026-09-19，08-27 事故遗留项落地）----
# ZT 虚拟网卡上会出现 0.0.0.0/0 via 25.255.255.254（ZT 假网关）metric 9999 的路由。它平时不生效，
# 但开机 DHCP 未完成、或 USB 网卡抖动导致真默认路由消失的那几十秒里，它就成了唯一默认路由：
# 流量进 ZT 黑洞（SYN 超时而不是立刻 network unreachable），隧道/坐席重连被拖慢 20s+，
# 探针本身也记成 GATEWAY LOST (gw=25.255.255.254)。allowDefault 已是 0 仍会出现，故每拍巡检删除。
# 只删「默认路由 + 下一跳=ZT 假网关」这一条；ZT 自身的 10.x/24 子网路由不碰，远程运维不受影响。
try {
  $ztDef = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Where-Object { $_.NextHop -eq '25.255.255.254' })
  if ($ztDef.Count -gt 0) {
    $ztDef | Remove-NetRoute -Confirm:$false -ErrorAction Stop
    $events += "SELF-HEAL: 删除 ZeroTier 兜底默认路由 0.0.0.0/0 via 25.255.255.254（{0} 条；开机/网卡抖动时它会把出口流量引进黑洞）" -f $ztDef.Count
  }
} catch {
  $events += "SELF-HEAL-FAILED: 删除 ZeroTier 默认路由失败：{0}" -f $_.Exception.Message
}

if (-not $prev) {
  $events += "probe v2 started: $line"
} else {
  if ($prev.wired -ne $cur.wired)   { $events += "WIRED {0} -> {1}" -f $prev.wired, $cur.wired }
  if ([bool]$prev.wifiUp -ne $cur.wifiUp -or $prev.ssid -ne $cur.ssid) { $events += "WIFI {0}/{1} -> {2}/{3}" -f $prev.wifiUp, $prev.ssid, $cur.wifiUp, $cur.ssid }
  if ([bool]$prev.gwUp -ne $cur.gwUp)   { $events += "GATEWAY {0} -> {1} (gw={2})" -f $(if ($prev.gwUp) {'reachable'} else {'LOST'}), $(if ($cur.gwUp) {'reachable'} else {'LOST'}), $gw }
  if ([bool]$prev.pubUp -ne $cur.pubUp) { $events += "INTERNET {0} -> {1}" -f $(if ($prev.pubUp) {'reachable'} else {'LOST'}), $(if ($cur.pubUp) {'reachable'} else {'LOST'}) }
  if ([bool]$prev.tcp176 -ne $cur.tcp176) { $events += "TCP176 {0} -> {1}" -f $(if ($prev.tcp176) {'ok'} else {'FAIL'}), $(if ($cur.tcp176) {'ok'} else {'FAIL'}) }
  if ($prev.gw -ne $cur.gw) { $events += "DEFAULT-GW {0} -> {1}" -f $prev.gw, $cur.gw }
  $gapMin = ($now - [datetime]$prev.ts).TotalMinutes
  if ($gapMin -gt 3) { $events += ("PROBE-GAP {0:N0} min（上一拍 {1}；机器关机/重启/任务未跑）" -f $gapMin, $prev.ts) }
}
foreach ($pl in $powerLoss) { $events += "POWER-LOSS-REBOOT: Kernel-Power 41 @ $pl（非正常关机后开机：掉电/死机/长按电源强制重启，非网络故障本身）" }
foreach ($e in $events) { Add-Content -Path $evlog -Value ("[{0}] {1}" -f $ts, $e) -Encoding UTF8 }

$cur | ConvertTo-Json -Compress | Set-Content -Path $stateFile -Encoding UTF8
