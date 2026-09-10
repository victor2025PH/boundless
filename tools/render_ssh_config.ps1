# 从 deploy/machines.json 生成统一 SSH config 片段（写入各机 ~/.ssh/config.d/boundless 或合并）
[CmdletBinding()]
param(
  [string]$OutFile = ""
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path (Split-Path $PSScriptRoot -Parent) -ErrorAction SilentlyContinue
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Machines = Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json

$keyMap = @{
  'id_ed25519'         = '~/.ssh/id_ed25519'
  'id_rsa'             = '~/.ssh/id_rsa'
  'cluster_controller' = '~/.ssh/cluster_controller'
}

$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine('# boundless cluster — generated from deploy/machines.json')
[void]$sb.AppendLine('# DO NOT hand-edit; re-run tools/render_ssh_config.ps1')
[void]$sb.AppendLine('')

foreach ($m in $Machines.machines) {
  $aliases = @($m.ssh)
  $idFile = $keyMap[[string]$m.key]
  if (-not $idFile) { $idFile = '~/.ssh/cluster_controller' }
  foreach ($a in $aliases) {
    [void]$sb.AppendLine("Host $a")
    [void]$sb.AppendLine("    HostName $($m.ip)")
    [void]$sb.AppendLine("    User $($m.user)")
    [void]$sb.AppendLine("    IdentityFile $idFile")
    [void]$sb.AppendLine("    IdentitiesOnly yes")
    [void]$sb.AppendLine("    StrictHostKeyChecking accept-new")
    [void]$sb.AppendLine("    ServerAliveInterval 30")
    [void]$sb.AppendLine("    ConnectTimeout 8")
    [void]$sb.AppendLine('')
  }
}

# VPS
# StdinNull yes（= ssh -n）是 VPS 两个块的必需项（2026-09-11 定案）：Windows 自带
# OpenSSH_for_Windows_9.5p1 在计划任务/无人值守控制台下跑一次性远程命令，命令越快越容易
# 在收尾时卡在 stdin 读取上永不退出（Win32-OpenSSH #1334）——duty_watch_loop 每晚几十次
# 「timeout>40s 重试才通」、ProdEdgeWatchdog / VisionTunnelWatchdog 09-10 挂死 18h 同源。
# 全仓对 VPS 的 ssh 没有任何一处经 stdin 送数据（scp 走独立通道、-N 隧道无 exec 通道），
# 所以按主机兜底是安全的；**别把它加进局域网机器块**——push_livewp.ps1 有 `tar | ssh`
# 管道，StdinNull 会让远端 tar 静默解出空包。
[void]$sb.AppendLine('Host vps-bd2026 bd2026')
[void]$sb.AppendLine('    HostName 165.154.233.121')
[void]$sb.AppendLine('    User ubuntu')
[void]$sb.AppendLine('    IdentityFile ~/.ssh/hualing_deploy')
[void]$sb.AppendLine('    IdentitiesOnly yes')
[void]$sb.AppendLine('    StrictHostKeyChecking accept-new')
[void]$sb.AppendLine('    ServerAliveInterval 30')
[void]$sb.AppendLine('    StdinNull yes')
[void]$sb.AppendLine('')
# 同一台 VPS 按裸 IP 访问的脚本（prod_edge_watchdog / vision_tunnel_watchdog / uplink_watchdog /
# *_tunnel.ps1 自带 -i vision_key 与 ubuntu@）：只补 StdinNull 与保活，不设 User/IdentityFile，
# 不覆盖脚本自己的命令行参数。
[void]$sb.AppendLine('Host 165.154.233.121')
[void]$sb.AppendLine('    StrictHostKeyChecking accept-new')
[void]$sb.AppendLine('    ServerAliveInterval 30')
[void]$sb.AppendLine('    StdinNull yes')
[void]$sb.AppendLine('')

$text = $sb.ToString()
if (-not $OutFile) {
  $OutFile = Join-Path $Root 'deploy\ssh_config.boundless'
}
[IO.File]::WriteAllText($OutFile, $text, [Text.UTF8Encoding]::new($false))
Write-Host "wrote $OutFile"
return $OutFile
