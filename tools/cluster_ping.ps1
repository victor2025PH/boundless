# 五机 SSH 网状 + GPU 服务探活
[CmdletBinding()]
param()
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Machines = (Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json).machines
$Hub = 'http://192.168.0.176:9000'

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
$urls = @(
  @{ n = 'Hub 幻声'; u = "$Hub/health" },
  @{ n = 'Faceswap 幻颜节点'; u = 'http://192.168.0.104:8000/health' },
  @{ n = 'STT 通传节点'; u = 'http://192.168.0.140:7854/health' },
  @{ n = 'EmotionTTS 通译'; u = 'http://192.168.0.117:7852/health' },
  @{ n = 'Qwen3TTS 通译'; u = 'http://192.168.0.117:7858/health' }
)
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
