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
# ⚠ 不要在任何 Host 块里写 `StdinNull yes`（2026-09-11 实锤后撤回）：Windows 自带 ssh.exe
# 9.5p1 的一次性命令挂死 bug（Win32-OpenSSH #1334）要靠 **每个调用点显式 `ssh -n`** 修，
# 不能按主机兜底——scp/sftp 是拉起 ssh 子进程、经它的 stdin/stdout 跑协议流，主机级
# StdinNull 会把子进程 stdin 接到 /dev/null，scp 立刻 `Connection closed`（本机 04:2x 实测），
# duty_watch_loop 下载诊断包 / tenant_ops _vps_push / chatx_gate 全部会断。
[void]$sb.AppendLine('Host vps-bd2026 bd2026')
[void]$sb.AppendLine('    HostName 165.154.233.121')
[void]$sb.AppendLine('    User ubuntu')
[void]$sb.AppendLine('    IdentityFile ~/.ssh/hualing_deploy')
[void]$sb.AppendLine('    IdentitiesOnly yes')
[void]$sb.AppendLine('    StrictHostKeyChecking accept-new')
[void]$sb.AppendLine('    ServerAliveInterval 30')
[void]$sb.AppendLine('')

$text = $sb.ToString()
if (-not $OutFile) {
  $OutFile = Join-Path $Root 'deploy\ssh_config.boundless'
}
[IO.File]::WriteAllText($OutFile, $text, [Text.UTF8Encoding]::new($false))
Write-Host "wrote $OutFile"
return $OutFile
