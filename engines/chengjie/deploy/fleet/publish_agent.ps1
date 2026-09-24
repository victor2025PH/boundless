# publish_agent.ps1 -- from the dev/build machine: upload Agent release + (optionally) controller tarball to the VPS.
#
#   powershell -File deploy\fleet\publish_agent.ps1                       # upload fleet_agent\dist\* -> https://bd2026.cc/downloads/fleet/
#   powershell -File deploy\fleet\publish_agent.ps1 -PackController      # also tar engines/chengjie -> ~/chatx-fleet-src.tar.gz on VPS
#   powershell -File deploy\fleet\publish_agent.ps1 -PackController -Deploy   # ... and run: sudo bash deploy_controller.sh (production change!)
#
# Requires: OpenSSH client (ssh/scp) with key auth to $SshHost. Never embeds tokens. ASCII only.
[CmdletBinding()]
param(
  [string]$SshHost = "ubuntu@bd2026.cc",
  [string]$RemoteDownloads = "/home/ubuntu/yuntech/public/downloads/fleet",
  [string]$PublicBase = "https://bd2026.cc/downloads/fleet",
  [string]$DistDir = "",
  [switch]$PackController,
  [switch]$Deploy,
  [switch]$BuildSetup,
  [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
function Say($m) { Write-Host "[publish] $m" }
function Run($cmd) { Say $cmd; if (-not $WhatIf) { Invoke-Expression $cmd; if ($LASTEXITCODE -ne 0) { throw "failed: $cmd" } } }

$engine = Resolve-Path (Join-Path $PSScriptRoot "..\..")
if (-not $DistDir) { $DistDir = Join-Path $engine "fleet_agent\dist" }
$mf = Join-Path $DistDir "manifest.json"
if (-not (Test-Path $mf)) { throw "no $mf -- run: python fleet_agent\build_agent.py --base-url $PublicBase/" }
$m = Get-Content $mf -Raw | ConvertFrom-Json
Say "agent $($m.version) sha256=$($m.sha256.Substring(0,12))... file=$($m.file)"
if ($m.url -notlike "$PublicBase/*") { Say "WARN manifest.url=$($m.url) does not start with $PublicBase (rebuild with --base-url)" }

Run "ssh $SshHost 'mkdir -p $RemoteDownloads'"
$setupScript = Join-Path $engine "fleet_agent\build_setup.ps1"
$iscc = $env:INNO_SETUP
if (-not $iscc) {
  foreach ($c in @(
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
  )) { if ($c -and (Test-Path -LiteralPath $c)) { $iscc = $c; break } }
}
if ($BuildSetup -or $iscc) {
  if (-not $iscc -and $BuildSetup) { throw "ISCC.exe not found. winget install --id JRSoftware.InnoSetup -e" }
  if ($iscc) {
    Say "building ChatXAgentSetup.exe"
    if (-not $WhatIf) { & powershell -NoProfile -File $setupScript -DistDir $DistDir; if ($LASTEXITCODE -ne 0) { throw "build_setup.ps1 failed" } }
  }
} else {
  Say "WARN Inno Setup (ISCC.exe) not found; public page will not get ChatXAgentSetup.exe. winget install --id JRSoftware.InnoSetup -e"
}
$names = @($m.file, "$($m.file).sha256", "manifest.json", "Install-ChatXAgent.ps1", "Uninstall-ChatXAgent.ps1", "ChatXAgentSetup.exe", "ChatXAgentSetup.exe.sha256")
if ($m.setup_file) { $names += @([string]$m.setup_file, ([string]$m.setup_file + ".sha256")) }
$files = $names | Select-Object -Unique | ForEach-Object { Join-Path $DistDir $_ } | Where-Object { Test-Path $_ }
# versioned copy keeps old installers downloadable for rollback: chatx-agent-<ver>.exe
Run ("scp " + (($files | ForEach-Object { '"' + $_ + '"' }) -join ' ') + " ${SshHost}:$RemoteDownloads/")
Run "ssh $SshHost 'cp -f $RemoteDownloads/$($m.file) $RemoteDownloads/chatx-agent-$($m.version).exe && ls -la $RemoteDownloads'"

if (-not $WhatIf) {
  try {
    $remote = Invoke-RestMethod -Uri "$PublicBase/manifest.json?t=$(Get-Date -UFormat %s)" -UseBasicParsing
    if ("$($remote.sha256)" -eq "$($m.sha256)") { Say "public manifest OK: $PublicBase/manifest.json -> $($remote.version)" }
    else { Say "WARN public manifest sha differs (CDN cache?)" }
  } catch { Say "WARN cannot fetch $PublicBase/manifest.json : $($_.Exception.Message)" }
}

if ($PackController) {
  $tar = Join-Path $env:TEMP "chatx-fleet-src.tar.gz"
  Say "packing controller source -> $tar"
  if (-not $WhatIf) {
    Push-Location $engine
    try {
      # --exclude=config drops secrets, then append config/presets so the fleet_control preset survives.
      # (GNU tar --exclude cannot "un-exclude" a child; append is the portable form, including Windows tar.)
      $raw = Join-Path $env:TEMP "chatx-fleet-src.tar"
      if (Test-Path $raw) { Remove-Item $raw -Force }
      & tar -cf $raw --exclude=.venv --exclude=node_modules --exclude=desktop --exclude=tests --exclude=sessions `
          --exclude=config --exclude=logs --exclude=data --exclude=__pycache__ --exclude=fleet_agent/dist --exclude=fleet_agent/build `
          --exclude=.git .
      if ($LASTEXITCODE -ne 0) { throw "tar failed" }
      if (Test-Path (Join-Path $engine "config\presets")) {
        & tar -rf $raw config/presets
        if ($LASTEXITCODE -ne 0) { throw "tar append config/presets failed" }
      }
      if (Test-Path $tar) { Remove-Item $tar -Force }
      Add-Type -AssemblyName System.IO.Compression
      $in = [System.IO.File]::OpenRead($raw)
      $out = [System.IO.File]::Create($tar)
      $gz = New-Object System.IO.Compression.GzipStream($out, [System.IO.Compression.CompressionMode]::Compress)
      try { $in.CopyTo($gz) } finally { $gz.Dispose(); $in.Dispose(); $out.Dispose(); Remove-Item $raw -Force -ErrorAction SilentlyContinue }
    } finally { Pop-Location }
  }
  Run "scp `"$tar`" `"$engine\deploy\fleet\deploy_controller.sh`" `"$engine\deploy\fleet\chatx-fleet.service`" `"$engine\deploy\fleet\nginx-fleet.conf`" ${SshHost}:~/"
  if ($Deploy) {
    Say "PRODUCTION CHANGE: running deploy_controller.sh on $SshHost"
    Run "ssh -t $SshHost 'sudo bash ~/deploy_controller.sh ~/chatx-fleet-src.tar.gz'"
  } else {
    Say "uploaded. On the VPS run:  sudo bash ~/deploy_controller.sh ~/chatx-fleet-src.tar.gz"
  }
}
Say "next: update fleet_control.download.* in /etc/chatx-fleet/config.yaml (version=$($m.version) installer_url=$($m.installer) sha256=$($m.sha256)) or leave /fleet/ page to manifest.json"
