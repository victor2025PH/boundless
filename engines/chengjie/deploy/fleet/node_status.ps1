# node_status.ps1 -- read a fleet node's agent status over ssh, readable on any console code page.
#
#   powershell -File deploy\fleet\node_status.ps1 -SshHost yuyan
#   powershell -File deploy\fleet\node_status.ps1 -SshHost yuyan -Cmd service-status
#
# The agent prints UTF-8 (chatx-agent.exe, and since P2 also source mode). A Chinese Windows
# console decodes ssh output as GBK (code page 936), so the Chinese labels turn into mojibake.
# This wrapper switches the local console to UTF-8 for the call and restores it afterwards.
# Read-only: only runs "status" or "service-status". ASCII only. No tokens.
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$SshHost,
  [string]$Exe = 'C:\Program Files\ChatX Agent\chatx-agent.exe',
  [string]$StateDir = 'C:\ProgramData\ChatX\fleet',
  [ValidateSet('status', 'service-status')][string]$Cmd = 'status'
)
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding $false
$prevOut = [Console]::OutputEncoding
$prevPipe = $OutputEncoding
try {
  [Console]::OutputEncoding = $utf8
  $OutputEncoding = $utf8
  # Windows PowerShell 5.1 (and pwsh "Legacy" arg passing) drops bare embedded quotes when calling
  # a native exe, so "C:\Program Files\..." would reach the node as C:\Program. Escape them there.
  $q = '"'
  $legacy = ($PSVersionTable.PSVersion.Major -le 5) -or ((Get-Variable PSNativeCommandArgumentPassing -ValueOnly -ErrorAction SilentlyContinue) -eq 'Legacy')
  if ($legacy) { $q = '\"' }
  $remote = $q + $Exe + $q + ' --state-dir ' + $q + $StateDir + $q + ' ' + $Cmd
  & ssh -o BatchMode=yes -o ConnectTimeout=10 $SshHost $remote
  $rc = $LASTEXITCODE
} finally {
  [Console]::OutputEncoding = $prevOut
  $OutputEncoding = $prevPipe
}
exit $rc
