# 识图反向隧道（117 -> VPS）：把本地 176 的 Ollama(11434) 暴露到 VPS 的 127.0.0.1:18411，
# 供官网网关 /api/ai/v1 的识图路由转发（VISION_RELAY_URL=http://127.0.0.1:18411/v1）。
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
    # -N 不执行远程命令；-R 反向转发；ExitOnForwardFailure 端口占用即退出重试
    & ssh -N -R 127.0.0.1:18411:192.168.0.176:11434 `
      -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
      -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15 `
      -i $key ubuntu@165.154.233.121 2>&1 | ForEach-Object { Log $_ }
  } catch {
    Log ("exception: " + $_)
  }
  Log 'tunnel dropped; reconnect in 10s'
  Start-Sleep -Seconds 10
}
