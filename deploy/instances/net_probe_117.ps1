# net_probe_117.ps1 — 117 网线体检探针（实施71 2026-08-27：网线更换前后对比取证）
# 计划任务 Boundless-net-probe-117 每 5 分钟一拍：物理网卡协商速率 + 三段 ping
# （网关=线本身 / 176=局域网段 / VPS=公网出口）。判读：换新线后 link 应从
# 100 Mbps 回到 1 Gbps；任一段 <5/5 = 该段有丢包。网线验收通过后本任务可删：
#   schtasks /Delete /TN Boundless-net-probe-117 /F
$ErrorActionPreference = "SilentlyContinue"
$log = "D:\chengjie-instances\.ops\net_probe_117.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 2MB)) { Move-Item $log "$log.1" -Force }
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$nic = Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Select-Object -First 1
$parts = @("link=" + $(if ($nic) { $nic.LinkSpeed } else { "down" }))
$gw = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" | Select-Object -First 1).NextHop
# ping.exe 而非 Test-Connection（pwsh7 的 Test-Connection 对 176 给过 10/10 的误导性
# 成功，原生 ping 实测 176 恒 100% loss——176 防火墙本就不回 ICMP，属特性非故障，
# 2026-08-27 06:25 三口径鉴别定案）。故 ICMP 腿只测 网关（网线判据本体）+ VPS（公网
# 出口）；176 用下方 TCP 业务口作判据。
foreach ($t in @($gw, "165.154.233.121")) {
  if (-not $t) { continue }
  $out = & ping.exe -n 5 -w 1500 $t 2>$null | Out-String
  $ok = if ($out -match "Received = (\d+)") { $Matches[1] }
        elseif ($out -match "已接收 = (\d+)") { $Matches[1] } else { "0" }
  $parts += "${t}=$ok/5"
}
# TCP 业务口径（176 ollama）：比 ICMP 更贴近真实调用路径
try {
  $c = New-Object Net.Sockets.TcpClient
  $ar = $c.BeginConnect("192.168.0.176", 11434, $null, $null)
  $tcpOk = $ar.AsyncWaitHandle.WaitOne(2000) -and $c.Connected
  $c.Close()
  $parts += "tcp176:11434=" + $(if ($tcpOk) { "ok" } else { "FAIL" })
} catch { $parts += "tcp176:11434=FAIL" }
Add-Content -Path $log -Value ("[$ts] " + ($parts -join "  "))
