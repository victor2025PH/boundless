# 托管租户反向隧道（117 -> VPS）：把本机租户实例端口暴露到 VPS 的 localhost，
# 供 VPS nginx 按子域反代（https://<slug>.bd2026.cc -> VPS 127.0.0.1:<port> -> 本机 <port>）。
#
# 与 vision_tunnel.ps1 同模式（一条 ssh 多 -R、断线 10s 重连、只挂 VPS localhost 不上公网），
# 但**独立进程/任务**——AI 中转隧道与租户隧道互不牵连；且端口清单外置：
#   D:\chengjie-instances\.ops\tenant_tunnel_ports.txt   （每行一个端口；# 注释）
# 加租户 = tenant_ops expose 追加端口 + 杀当前 ssh（pid 文件）→ 本循环按新清单重连（~10s）。
# 清单为空时不起 ssh，30s 轮询等待（零租户暴露时零连接）。
#
# 安装（人工决定；与 VisionTunnel 同款 SYSTEM 常驻）：
#   schtasks /Create /TN TenantTunnel /TR "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File D:\boundless\deploy\instances\tenant_tunnel.ps1" /SC ONSTART /RU SYSTEM /F
#   schtasks /Run /TN TenantTunnel
$ErrorActionPreference = 'Continue'
$key   = 'D:\chengjie-instances\.ops\vision_key'   # 复用集群机器 key（ACL 仅 SYSTEM/Administrators）
$ports = 'D:\chengjie-instances\.ops\tenant_tunnel_ports.txt'
$pidf  = 'D:\chengjie-instances\.ops\tenant_tunnel.pid'
$log   = 'D:\chengjie-instances\.ops\tenant_tunnel.log'
$vps   = 'ubuntu@165.154.233.121'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Log($m) { ("{0} {1}" -f (Get-Date -Format s), $m) | Out-File $log -Append -Encoding utf8 }

# 日志自轮转（>5MB 截断保尾）
try {
  if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
    Get-Content $log -Tail 2000 | Set-Content ($log + '.1') -Encoding utf8
    Remove-Item $log -Force
  }
} catch {}

# 单例守卫：双跑会互抢远端端口，且配合下方僵尸清理会互杀健康会话（flap 战）。
$runnerMutex = New-Object System.Threading.Mutex($false, 'Global\TenantTunnelRunner')
$mutexOwned = $false
try { $mutexOwned = $runnerMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $mutexOwned = $true }
if (-not $mutexOwned) { Log 'duplicate runner detected (mutex busy); exiting'; exit 0 }

# 2026-08-12 僵尸监听自清：WAN 抖断后 VPS sshd 可能攥着死会话的远端转发监听，
# 重连全被 "remote port forwarding failed" 拒（实测卡 10-20 分钟）。命中该错误
# 即上 VPS 杀掉陈旧持有者后快速重连；VPS 侧 sshd ClientAliveInterval 30x3 兜底。
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
      '-i' $key $vps ("sudo fuser -k " + $spec + " >/dev/null 2>&1; true") 2>$null | Out-Null
}

Log 'tenant tunnel runner start'
while ($true) {
  $list = @()
  if (Test-Path $ports) {
    $list = @(Get-Content $ports | ForEach-Object { $_.Trim() } |
              Where-Object { $_ -match '^\d+$' } | Select-Object -Unique)
  }
  if (-not $list.Count) {
    Start-Sleep -Seconds 8   # 空清单轮询要快：expose 追加端口后最迟 ~8s 拉起首连
    continue
  }
  $fwd = @()
  foreach ($p in $list) { $fwd += @('-R', "127.0.0.1:${p}:127.0.0.1:${p}") }
  # -C 压缩：租户后台页面 1~2MB 未压缩 HTML 裸传过 WAN 隧道是「后台载入慢」的根因
  # （2026-08-07 实测：直连 localhost 渲染 0.03s，走隧道 18~49s）。zlib 压缩 HTML ~8-10x，
  # 隧道传输量随之骤降；SSH 压缩 CPU 开销可忽略。
  $args = @('-N', '-C') + $fwd + @(
    '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3', '-o', 'StrictHostKeyChecking=no',
    '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-i', $key, $vps)
  Log ("connect: ports " + ($list -join ','))
  try {
    $p = Start-Process -FilePath 'ssh' -ArgumentList $args -NoNewWindow -PassThru `
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
    Clear-StaleForwards $list
    Log 'tunnel dropped (stale bind); cleanup done, reconnect in 3s'
    Start-Sleep -Seconds 3
    continue
  }
  Log 'tunnel dropped/killed; reconnect in 10s'
  Start-Sleep -Seconds 10
}
