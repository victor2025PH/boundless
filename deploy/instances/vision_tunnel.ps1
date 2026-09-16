# AI 算力反向隧道（117 -> VPS）：把 LAN GPU 服务暴露到 VPS 的 localhost，
# 供官网网关 /api/ai/* 各路由转发（双活，网关按序试 + 失败冷却降权）：
#   176:11434 -> VPS 127.0.0.1:18411   识图 VLM 主（VISION_RELAY_URLS）
#   140:11434 -> VPS 127.0.0.1:18412   识图 VLM 备/分流
#   104:7865  -> VPS 127.0.0.1:18413   克隆 TTS 主（TTS_RELAY_URLS；2026-08-29 从
#                117:7852 改指 104 IndexTTS-2——智聊 TTS 主力迁 104、117 CosyVoice
#                已退役(EmotionTTS_Boot Disabled)，旧指向=外网克隆全灭的根因之一）
#   140:7852  -> VPS 127.0.0.1:18414   克隆 TTS 备（CosyVoice；断电后未回，AvatarHub 属地）
#   198:8765  -> VPS 127.0.0.1:18415   GPU ASR（ASR_RELAY_URLS，须带 /v1 后缀；
#                2026-08-30 从 176:8765 改指 198——ASR 单点断电后迁 198（whisper
#                large-v3-turbo cuda + SER），旧指向=全网转录瘫 7h 的根因，同 TTS 18413 病）
#   176:8767  -> VPS 127.0.0.1:18416   人脸嵌入边车（FACE_RELAY_URLS，不带 /v1；#333 视觉
#                身份层 2026-09-17，CPU onnx，客户图里是谁——网关 /api/ai/v1/face/embed）
# 对应 VPS env：VISION_RELAY_URLS=http://127.0.0.1:18411/v1,http://127.0.0.1:18412/v1
#              TTS_RELAY_URLS=http://127.0.0.1:18413,http://127.0.0.1:18414
#              ASR_RELAY_URLS=http://127.0.0.1:18415/v1
#              FACE_RELAY_URLS=http://127.0.0.1:18416
# 一条 ssh 承载全部 -R：少常驻进程，断线一起重连，watchdog 杀/拉一次全恢复。
#
# 只暴露到 VPS 的 localhost（非公网端口）；访问由网关的设备令牌鉴权把关，GPU 不直接对公网。
# BatchMode=yes 绝不交互卡住；断线自动 10s 重连；作为计划任务常驻（VisionTunnel）。
#
# 安装：schtasks /Create /TN VisionTunnel /TR "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File D:\boundless\deploy\instances\vision_tunnel.ps1" /SC ONSTART /RU SYSTEM /F ; schtasks /Run /TN VisionTunnel
$ErrorActionPreference = 'Continue'
# 机器专用 key（ACL 仅 SYSTEM/Administrators 可读）——SYSTEM 跑计划任务时
# OpenSSH 才不报「permissions too open」。用户 .ssh 下的 key 由 SYSTEM 读会被拒。
$key = 'D:\chengjie-instances\.ops\vision_key'
$log = 'D:\chengjie-instances\.ops\vision_tunnel.log'
$vps = 'ubuntu@165.154.233.121'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Log($m) { ("{0} {1}" -f (Get-Date -Format s), $m) | Out-File $log -Append -Encoding utf8 }

# 单例守卫：双跑会互抢远端端口，且配合下方僵尸清理会互杀健康会话（flap 战）。
$runnerMutex = New-Object System.Threading.Mutex($false, 'Global\VisionTunnelRunner')
$mutexOwned = $false
try { $mutexOwned = $runnerMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $mutexOwned = $true }
if (-not $mutexOwned) { Log 'duplicate runner detected (mutex busy); exiting'; exit 0 }

# 2026-08-12 僵尸监听自清：WAN 抖断后 VPS sshd 可能攥着死会话的远端转发监听，
# 重连全被 "remote port forwarding failed" 拒（8/3、8/7、8/8、8/12 实测卡 10-20
# 分钟，watchdog 杀本地 ssh 救不了远端）。命中该错误即上 VPS 杀掉陈旧持有者后
# 快速重连；VPS 侧 sshd ClientAliveInterval 30x3 兜底。
$tunnelPorts = @('18411','18412','18413','18414','18415','18416')
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

Log 'vision tunnel runner start'
while ($true) {
  $script:bindFail = $false
  try {
    # -N 不执行远程命令；-R 反向转发（识图 176/140 + 克隆TTS 117/140 + GPU ASR 176）；
    # ExitOnForwardFailure 任一端口占用即退出重试（僵尸占端口由下方自清收割）
    & ssh -N -R 127.0.0.1:18411:192.168.0.176:11434 -R 127.0.0.1:18412:192.168.0.140:11434 `
      -R 127.0.0.1:18413:192.168.0.104:7865 -R 127.0.0.1:18414:192.168.0.140:7852 `
      -R 127.0.0.1:18415:192.168.0.198:8765 -R 127.0.0.1:18416:192.168.0.176:8767 `
      -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
      -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15 `
      -i $key $vps 2>&1 | ForEach-Object {
        Log $_
        if ("$_" -match 'remote port forwarding failed') { $script:bindFail = $true }
      }
  } catch {
    Log ("exception: " + $_)
  }
  if ($script:bindFail -and (((Get-Date) - $lastStaleCleanup).TotalSeconds -gt 60)) {
    $lastStaleCleanup = Get-Date
    Clear-StaleForwards $tunnelPorts
    Log 'tunnel dropped (stale bind); cleanup done, reconnect in 3s'
    Start-Sleep -Seconds 3
    continue
  }
  Log 'tunnel dropped; reconnect in 10s'
  Start-Sleep -Seconds 10
}
