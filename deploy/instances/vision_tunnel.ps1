# AI 算力反向隧道（117 -> VPS）：把 LAN GPU 服务暴露到 VPS 的 localhost，
# 供官网网关 /api/ai/* 各路由转发（双活，网关按序试 + 失败冷却降权）：
#   176:11434 -> VPS 127.0.0.1:18411   识图 VLM 主（VISION_RELAY_URLS）
#   140:11434 -> VPS 127.0.0.1:18412   识图 VLM 备/分流
#   117:7852  -> VPS 127.0.0.1:18413   克隆 TTS 主（TTS_RELAY_URLS，2026-08-03）
#   140:7852  -> VPS 127.0.0.1:18414   克隆 TTS 备
#   176:8765  -> VPS 127.0.0.1:18415   GPU ASR（ASR_RELAY_URLS，须带 /v1 后缀）
# 对应 VPS env：VISION_RELAY_URLS=http://127.0.0.1:18411/v1,http://127.0.0.1:18412/v1
#              TTS_RELAY_URLS=http://127.0.0.1:18413,http://127.0.0.1:18414
#              ASR_RELAY_URLS=http://127.0.0.1:18415/v1
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
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Log($m) { ("{0} {1}" -f (Get-Date -Format s), $m) | Out-File $log -Append -Encoding utf8 }

Log 'vision tunnel runner start'
while ($true) {
  try {
    # -N 不执行远程命令；-R 反向转发（识图 176/140 + 克隆TTS 117/140 + GPU ASR 176）；
    # ExitOnForwardFailure 任一端口占用即退出重试（僵尸占端口由 watchdog 收割）
    & ssh -N -R 127.0.0.1:18411:192.168.0.176:11434 -R 127.0.0.1:18412:192.168.0.140:11434 `
      -R 127.0.0.1:18413:127.0.0.1:7852 -R 127.0.0.1:18414:192.168.0.140:7852 `
      -R 127.0.0.1:18415:192.168.0.176:8765 `
      -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
      -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15 `
      -i $key ubuntu@165.154.233.121 2>&1 | ForEach-Object { Log $_ }
  } catch {
    Log ("exception: " + $_)
  }
  Log 'tunnel dropped; reconnect in 10s'
  Start-Sleep -Seconds 10
}
