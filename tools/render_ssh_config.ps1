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
