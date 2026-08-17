# Render unified SSH config fragment from deploy/machines.json (v2 schema).
# ASCII-ONLY source on purpose (repo discipline: .ps1 with CJK needs BOM; we avoid the problem).
# Active aliases come from machines[].ssh; legacy aliases from machines[].ssh_deprecated
# are rendered in a marked compat section (sunset date below) so muscle memory survives renames.
[CmdletBinding()]
param(
  [string]$OutFile = ""
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Data = Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json

$keyMap = @{
  'id_ed25519'         = '~/.ssh/id_ed25519'
  'id_rsa'             = '~/.ssh/id_rsa'
  'cluster_controller' = '~/.ssh/cluster_controller'
}

function Add-HostBlock([System.Text.StringBuilder]$sb, [string]$alias, $m, [string]$idFile) {
  [void]$sb.AppendLine("Host $alias")
  [void]$sb.AppendLine("    HostName $($m.ip)")
  [void]$sb.AppendLine("    User $($m.user)")
  [void]$sb.AppendLine("    IdentityFile $idFile")
  [void]$sb.AppendLine("    IdentitiesOnly yes")
  [void]$sb.AppendLine("    StrictHostKeyChecking accept-new")
  [void]$sb.AppendLine("    ServerAliveInterval 30")
  [void]$sb.AppendLine("    ConnectTimeout 8")
  [void]$sb.AppendLine('')
}

$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine('# boundless cluster - generated from deploy/machines.json (v2 ' + [string]$Data.version + ')')
[void]$sb.AppendLine('# DO NOT hand-edit; re-run tools/render_ssh_config.ps1')
[void]$sb.AppendLine('# naming: id = function-pinyin (zhongshu/yunsheng/shengbei/lianbei/tingxie/kouxing)')
[void]$sb.AppendLine('')

foreach ($m in $Data.machines) {
  $idFile = $keyMap[[string]$m.key]
  if (-not $idFile) { $idFile = '~/.ssh/cluster_controller' }
  [void]$sb.AppendLine("# -- $($m.id) $($m.ip) [$($m.gpu)] --")
  foreach ($a in @($m.ssh)) {
    Add-HostBlock $sb ([string]$a) $m $idFile
  }
}

[void]$sb.AppendLine('# --- legacy aliases (pre-2026-08-05 machine names; sunset 2026-11-05, then removed) ---')
[void]$sb.AppendLine('')
foreach ($m in $Data.machines) {
  $idFile = $keyMap[[string]$m.key]
  if (-not $idFile) { $idFile = '~/.ssh/cluster_controller' }
  $dep = @()
  if ($m.PSObject.Properties.Name -contains 'ssh_deprecated') { $dep = @($m.ssh_deprecated) }
  foreach ($a in $dep) {
    Add-HostBlock $sb ([string]$a) $m $idFile
  }
}

# VPS (website)
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
