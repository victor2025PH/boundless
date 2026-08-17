# Deploy Deskflow soft-KVM across the DEV boxes (server = hub seat, clients = typing boxes).
# ASCII-ONLY source. v1 scope decision (2026-08-05): only machines where humans actually type
# (zhongshu / shengbei / kouxing) join the KVM grid; pure compute nodes (yunsheng / lianbei /
# tingxie) are deliberately excluded so a stray mouse can never click into a production box
# mid-livestream. Add a machine later = add screen+link to deskflow-server.conf and rerun
# this script with -Machines <id>.
# TLS (P3 2026-08-06): self-signed cert at C:\Users\Public\boundless-hud\deskflow.pem
# (openssl, sha256 fp logged in deploy output). tlsEnabled=true + checkPeerFingerprints=false
# = wire encryption without interactive fingerprint confirmation (headless clients).
# Rollback: rerun with -NoTls.
[CmdletBinding()]
param(
  [string[]]$Machines = @('zhongshu', 'shengbei', 'kouxing'),
  [string]$ServerId = 'zhongshu',
  [switch]$NoTls
)
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Data = Get-Content (Join-Path $Root 'deploy\machines.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$HudBase = 'C:\Users\Public\boundless-hud'
$Exe = 'C:\Program Files\Deskflow\deskflow-core.exe'
$MsiLocal = 'C:\Users\Public\deskflow.msi'
$VcLocal = 'C:\Users\Public\vc_redist18.x64.exe'
$ServerIp = ($Data.machines | Where-Object { $_.id -eq $ServerId }).ip

function Get-M([string]$id) { return ($Data.machines | Where-Object { $_.id -eq $id } | Select-Object -First 1) }

function New-SettingsIni([string]$id, [bool]$isServer) {
  # QSettings INI treats backslash as escape char -> forward slashes (Qt is fine with them on Windows)
  $fwd = $HudBase -replace '\\', '/'
  $lines = @('[core]', "screenName=$id", 'port=24800')
  if ($isServer) {
    $lines += @('', '[server]', 'externalConfig=true',
      "externalConfigFile=$fwd/deskflow-server.conf")
  } else {
    $lines += @('', '[client]', "remoteHost=$ServerIp")
  }
  if ($NoTls) {
    $lines += @('', '[security]', 'tlsEnabled=false')
  } else {
    # deskflow TLS is mutual-ish: the CLIENT also refuses to handshake without its own cert
    # ("could not load client certificates", 2026-08-06 shengbei foreground proof), so every
    # machine gets its own pem and its own certificate= key.
    $lines += @('', '[security]', 'tlsEnabled=true', 'checkPeerFingerprints=false',
      "certificate=$fwd/deskflow.pem")
  }
  return ($lines -join "`r`n") + "`r`n"
}

# server topology: shengbei sits to the RIGHT of zhongshu, kouxing to the LEFT (edit to taste)
$ServerConf = @(
  'section: screens'
  "`t${ServerId}:"
  ($Machines | Where-Object { $_ -ne $ServerId } | ForEach-Object { "`t${_}:" })
  'end'
  'section: links'
  "`t${ServerId}:"
  "`t`tright = shengbei"
  "`t`tleft = kouxing"
  "`tshengbei:"
  "`t`tleft = ${ServerId}"
  "`tkouxing:"
  "`t`tright = ${ServerId}"
  'end'
  'section: options'
  "`tswitchDelay = 300"
  "`tswitchCornerSize = 30"
  'end'
) | ForEach-Object { $_ } | Out-String

$Launcher = @(
  "param([string]`$Mode = 'client')"
  "Start-Process -WindowStyle Hidden -FilePath '$Exe' -ArgumentList @(`$Mode, '-s', '$HudBase\deskflow-settings.conf')"
) -join "`r`n"

$RegTask = @(
  "param([string]`$Mode = 'client')"
  "`$tr = 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\Public\boundless-hud\deskflow_launch.ps1 -Mode ' + `$Mode"
  "schtasks /create /f /tn BoundlessDeskflow /sc onlogon /tr `$tr | Out-Null"
  "schtasks /run /tn BoundlessDeskflow | Out-Null"
  "Write-Output 'DESKFLOW_TASK_OK'"
) -join "`r`n"

$tmp = Join-Path $env:TEMP 'deskflow_stage'
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

function New-MachineCert([string]$id) {
  # per-machine self-signed pem (key+cert concatenated, synergy style).
  # cached OUTSIDE any repo and outside %TEMP%: clients TOFU-pin the server fingerprint,
  # so regenerating the server cert on every deploy would strand every client
  # ("fingerprint does not match trusted fingerprint", kouxing 2026-08-06 proof).
  $cache = 'C:\Users\user\.ssh\deskflow_certs'
  New-Item -ItemType Directory -Force -Path $cache | Out-Null
  $pem = Join-Path $cache ("deskflow_{0}.pem" -f $id)
  if (Test-Path $pem) { return $pem }
  $ssl = 'D:\Miniconda3\Library\bin\openssl.exe'
  if (-not (Test-Path $ssl)) { $ssl = 'openssl' }
  if (-not $env:OPENSSL_CONF -or -not (Test-Path $env:OPENSSL_CONF)) {
    $cnf = 'D:\Miniconda3\Library\mingw64\etc\ssl\openssl.cnf'
    if (Test-Path $cnf) { $env:OPENSSL_CONF = $cnf }
  }
  $key = Join-Path $tmp ("k_{0}.pem" -f $id)
  $crt = Join-Path $tmp ("c_{0}.pem" -f $id)
  & $ssl req -x509 -newkey rsa:2048 -keyout $key -out $crt -days 3650 -nodes -subj ("/CN=deskflow-{0}" -f $id) 2>$null
  Get-Content $key, $crt | Set-Content $pem -Encoding ascii
  Remove-Item $key, $crt -Force -ErrorAction SilentlyContinue
  return $pem
}
[IO.File]::WriteAllText((Join-Path $tmp 'deskflow_launch.ps1'), $Launcher, [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText((Join-Path $tmp 'deskflow_regtask.ps1'), $RegTask, [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText((Join-Path $tmp 'deskflow-server.conf'), $ServerConf, [Text.UTF8Encoding]::new($false))

foreach ($id in $Machines) {
  $m = Get-M $id
  if (-not $m) { Write-Host "unknown machine $id" -ForegroundColor Red; continue }
  $alias = [string]$m.ssh[0]
  $isServer = ($id -eq $ServerId)
  $mode = if ($isServer) { 'server' } else { 'client' }
  Write-Host ("===== {0} ({1}) mode={2} =====" -f $id, $m.ip, $mode) -ForegroundColor Green

  $ini = New-SettingsIni $id $isServer
  [IO.File]::WriteAllText((Join-Path $tmp 'deskflow-settings.conf'), $ini, [Text.UTF8Encoding]::new($false))

  # 1) ensure installed (vc redist 14.50+ then msi); loopback ssh gives the elevated token
  $chk = "if (Test-Path '$Exe') { 'INSTALLED' } else { 'MISSING' }"
  $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($chk))
  $st = (ssh -o BatchMode=yes -o ConnectTimeout=12 $alias "powershell -NoProfile -EncodedCommand $b64" 2>&1) -join ''
  if ($st -notmatch 'INSTALLED') {
    Write-Host '-- installing (vc redist + msi) --'
    if (-not $isServer) {
      scp -o BatchMode=yes $MsiLocal ("{0}:C:/Users/Public/deskflow.msi" -f $alias) 2>&1 | Out-Null
      scp -o BatchMode=yes $VcLocal ("{0}:C:/Users/Public/vc_redist18.x64.exe" -f $alias) 2>&1 | Out-Null
    }
    $inst = "`$p = Start-Process 'C:\Users\Public\vc_redist18.x64.exe' -ArgumentList '/install','/quiet','/norestart' -Wait -PassThru; " +
            "`$p2 = Start-Process msiexec -ArgumentList '/i','C:\Users\Public\deskflow.msi','/qn','/norestart' -Wait -PassThru; " +
            "Write-Output ('VC=' + `$p.ExitCode + ' MSI=' + `$p2.ExitCode + ' OK=' + (Test-Path '$Exe'))"
    $b64i = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inst))
    ssh -o BatchMode=yes -o ConnectTimeout=300 $alias "powershell -NoProfile -EncodedCommand $b64i" 2>&1
  } else { Write-Host 'already installed' }

  # 2) ship config + launcher + task registrar (+ TLS cert)
  ssh -o BatchMode=yes -o ConnectTimeout=12 $alias "cmd /c if not exist $HudBase mkdir $HudBase" 2>&1 | Out-Null
  scp -o BatchMode=yes (Join-Path $tmp 'deskflow-settings.conf') ("{0}:C:/Users/Public/boundless-hud/deskflow-settings.conf" -f $alias) 2>&1 | Out-Null
  scp -o BatchMode=yes (Join-Path $tmp 'deskflow_launch.ps1') ("{0}:C:/Users/Public/boundless-hud/deskflow_launch.ps1" -f $alias) 2>&1 | Out-Null
  scp -o BatchMode=yes (Join-Path $tmp 'deskflow_regtask.ps1') ("{0}:C:/Users/Public/boundless-hud/deskflow_regtask.ps1" -f $alias) 2>&1 | Out-Null
  if (-not $NoTls) {
    $pem = New-MachineCert $id
    scp -o BatchMode=yes $pem ("{0}:C:/Users/Public/boundless-hud/deskflow.pem" -f $alias) 2>&1 | Out-Null
  }
  ssh -o BatchMode=yes -o ConnectTimeout=12 $alias "taskkill /IM deskflow-core.exe /F" 2>&1 | Out-Null
  if ($isServer) {
    scp -o BatchMode=yes (Join-Path $tmp 'deskflow-server.conf') ("{0}:C:/Users/Public/boundless-hud/deskflow-server.conf" -f $alias) 2>&1 | Out-Null
    Write-Host '-- firewall 24800 (LAN only) --'
    $fw = "netsh advfirewall firewall delete rule name='Deskflow 24800 LAN' | Out-Null; " +
          "netsh advfirewall firewall add rule name='Deskflow 24800 LAN' dir=in action=allow protocol=TCP localport=24800 remoteip=192.168.0.0/24"
    $b64f = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($fw))
    ssh -o BatchMode=yes -o ConnectTimeout=20 $alias "powershell -NoProfile -EncodedCommand $b64f" 2>&1 | Select-Object -Last 1
  }

  # 3) register + start
  ssh -o BatchMode=yes -o ConnectTimeout=30 $alias "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Public\boundless-hud\deskflow_regtask.ps1 -Mode $mode" 2>&1 |
    Where-Object { $_ -match 'DESKFLOW_TASK_OK|ERROR' }
}

Write-Host 'deploy_deskflow done; verify with: netstat server 24800 ESTABLISHED x clients' -ForegroundColor Cyan
