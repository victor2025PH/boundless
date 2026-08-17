# Generate Windows Terminal profile fragment for the cluster (hub seat convenience).
# One colored profile per machine: tab color = machine accent from deploy/machines.json,
# so "which box am I typing into" is answered by tab color before you read anything.
# Fragment lands at %LOCALAPPDATA%\Microsoft\Windows Terminal\Fragments\boundless\cluster.json
# (restart Windows Terminal to pick it up). ASCII-ONLY source; CJK names come from machines.json.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Data = Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$LocalIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -like '192.168.*' } | Select-Object -First 1).IPAddress

$profiles = @()
foreach ($m in $Data.machines) {
  $isLocal = ($m.ip -eq $LocalIp)
  $cmd = if ($isLocal) { 'powershell -NoLogo' } else { 'ssh ' + [string]$m.ssh[0] }
  $suffix = if ($isLocal) { ' (local)' } else { '' }
  $profiles += [ordered]@{
    name                     = ('{0} {1}{2}' -f $m.zh, $m.ssh[0], $suffix)
    commandline              = $cmd
    tabColor                 = [string]$m.accent
    suppressApplicationTitle = $true
    tabTitle                 = ('{0} {1}' -f $m.zh, $m.ip)
  }
}
$doc = @{ profiles = $profiles } | ConvertTo-Json -Depth 4
$dir = Join-Path $env:LOCALAPPDATA 'Microsoft\Windows Terminal\Fragments\boundless'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$out = Join-Path $dir 'cluster.json'
[IO.File]::WriteAllText($out, $doc, [Text.UTF8Encoding]::new($false))
Write-Host "wrote $out ($($profiles.Count) profiles); restart Windows Terminal to load"
