param([Parameter(Mandatory)][string]$PubsFile)
$ErrorActionPreference = 'Stop'
$pubs = Get-Content $PubsFile -Encoding ascii | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$akUser = Join-Path $env:USERPROFILE '.ssh\authorized_keys'
$akAdmin = 'C:\ProgramData\ssh\administrators_authorized_keys'
if (-not (Test-Path (Split-Path $akUser))) { New-Item -ItemType Directory -Path (Split-Path $akUser) -Force | Out-Null }
foreach ($ak in @($akUser, $akAdmin)) {
  $lines = @()
  if (Test-Path $ak) { $lines = Get-Content $ak }
  foreach ($p in $pubs) {
    if ($lines -notcontains $p) {
      Add-Content -Path $ak -Value $p -Encoding ascii
      Write-Output "ADDED $ak"
    }
  }
  if (Test-Path $ak) {
    icacls $ak /inheritance:r | Out-Null
    icacls $ak /grant 'NT AUTHORITY\SYSTEM:(F)' | Out-Null
    icacls $ak /grant 'BUILTIN\Administrators:(F)' | Out-Null
  }
}
Write-Output 'KEYS_OK'
