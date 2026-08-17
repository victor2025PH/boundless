# 六机 SSH 网状 + GPU 服务探活（服务清单由 deploy/machines.json 生成，不再手抄——2026-08-05 v2）
[CmdletBinding()]
param()
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Data = Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$Machines = $Data.machines
$Hub = ('http://{0}:{1}' -f $Data.hub.ip, $Data.hub.port)

Write-Host "=== SSH mesh ===" -ForegroundColor Cyan
$sshOk = 0; $sshFail = 0
foreach ($m in $Machines) {
  $a = [string]$m.ssh[0]
  $o = ssh -o BatchMode=yes -o ConnectTimeout=6 $a "hostname" 2>&1
  if ($LASTEXITCODE -eq 0) {
    Write-Host ("[OK]  {0,-16} {1,-18} -> {2}" -f $m.zh, $a, ($o | Select-Object -First 1)) -ForegroundColor Green
    $sshOk++
  } else {
    Write-Host ("[FAIL]{0,-16} {1,-18} -> {2}" -f $m.zh, $a, ($o | Select-Object -First 1)) -ForegroundColor Red
    $sshFail++
  }
}

Write-Host "`n=== GPU / service health (from this host) ===" -ForegroundColor Cyan
# Hub 聚合健康 + 各非中枢机的服务 /health（中枢本机服务由 Hub /health 内部聚合，
# 且部分只绑 127.0.0.1，从局域网直探会误报——2026-08-05 实锤 :7900）
$urls = @(@{ n = ('Hub ' + ($Machines | Where-Object { $_.ip -eq $Data.hub.ip } | Select-Object -First 1).zh); u = "$Hub/health" })
foreach ($m in $Machines) {
  if ($m.ip -eq $Data.hub.ip) { continue }
  foreach ($svc in @($m.services)) {
    $parts = ([string]$svc) -split ':'
    if ($parts.Count -lt 2) { continue }
    $path = if ($parts[0] -like 'ollama*') { '/api/version' } else { '/health' }
    $urls += @{ n = ('{0} {1}' -f $m.zh, $parts[0]); u = ('http://{0}:{1}{2}' -f $m.ip, $parts[1], $path) }
  }
}
$svcOk = 0; $svcFail = 0
foreach ($x in $urls) {
  try {
    $r = Invoke-WebRequest -Uri $x.u -UseBasicParsing -TimeoutSec 4
    Write-Host ("[OK]  {0,-22} {1} -> {2}" -f $x.n, $x.u, [int]$r.StatusCode) -ForegroundColor Green
    $svcOk++
  } catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code) {
      Write-Host ("[HTTP]{0,-22} {1} -> {2}" -f $x.n, $x.u, $code) -ForegroundColor Yellow
      $svcOk++
    } else {
      Write-Host ("[DOWN]{0,-22} {1}" -f $x.n, $x.u) -ForegroundColor DarkYellow
      $svcFail++
    }
  }
}

Write-Host "`n=== workdir spot-check via SSH ===" -ForegroundColor Cyan
foreach ($m in $Machines) {
  $a = [string]$m.ssh[0]
  $zh = $m.zh
  $script = @'
$devName = ([string]([char]0x5F00) + [char]0x53D1)
$roots=@("D:\boundless","C:\boundless")
$devs=@((Join-Path "D:\" $devName),(Join-Path "C:\" $devName))
$wb=$roots | Where-Object { Test-Path (Join-Path $_ ".git") } | Select-Object -First 1
$db=$devs | Where-Object { Test-Path $_ } | Select-Object -First 1
$link = if ($db) { Get-ChildItem $db -Directory -ErrorAction SilentlyContinue | Select-Object -First 3 -ExpandProperty Name } else { @() }
Write-Output ("work=" + $wb + "; dev=" + $db + "; links=" + ($link -join ","))
'@
  $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
  $o = ssh -o BatchMode=yes -o ConnectTimeout=8 $a "powershell -NoProfile -EncodedCommand $b64" 2>&1
  Write-Host ("{0}: {1}" -f $zh, (($o | Where-Object { $_ -notmatch 'CLIXML|progress' }) -join ' '))
}

Write-Host "`nSUMMARY ssh_ok=$sshOk ssh_fail=$sshFail svc_ok=$svcOk svc_down=$svcFail" -ForegroundColor Cyan
if ($sshFail -gt 0) { exit 1 } else { exit 0 }
