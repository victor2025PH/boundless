# publish_agent.ps1 -- from the dev/build machine: upload Agent release + (optionally) controller tarball to the VPS.
#
#   powershell -File deploy\fleet\publish_agent.ps1                       # upload fleet_agent\dist\* -> https://bd2026.cc/downloads/fleet/
#   powershell -File deploy\fleet\publish_agent.ps1 -PackController      # ONLY tar engines/chengjie -> ~/chatx-fleet-src.tar.gz on VPS (no agent upload)
#   ... -PackController -PublishAgent                                     # controller tarball AND agent release in one run (explicit)
#   powershell -File deploy\fleet\publish_agent.ps1 -PackController -Deploy   # ... and run: sudo bash deploy_controller.sh (production change!)
#   powershell -File deploy\fleet\publish_agent.ps1 -BuildSetup         # rebuild ChatXAgentSetup.exe first (explicit only)
#   ... -RequireSigned                                                    # refuse to upload an unsigned ChatXAgentSetup.exe
#   ... -WhatIf                                                           # dry run: print every ssh/scp/sudo step, render the page locally
#   ... -NoMirror                                                         # skip the /var/www/dl-mirror sync (not recommended)
#
# ChatXAgentSetup.exe is rebuilt ONLY with -BuildSetup. Having ISCC installed no longer
# triggers a rebuild, so a hand-signed installer in dist\ is uploaded as is. A rebuild
# over a validly signed installer is refused unless -OverwriteSigned is also given.
# Before any upload the installer hash must match manifest.setup_sha256 and its .sha256 file.
#
# After the upload the versioned files are installed into the nginx mirror $MirrorDir
# (chatx-agent-<ver>.exe, ChatXAgentSetup-<ver>.exe, matching .sha256 files) and the public
# download page index.html is regenerated from the real files by render_download_page.py
# (version, size and SHA-256 are never typed by hand). The old index.html is kept as
# index.html.bak_<timestamp>. A versioned file already in the mirror with other bytes is refused.
#
# Runs under Windows PowerShell 5.1 (powershell.exe). Started from pwsh 7 it re-runs itself under
# powershell.exe with a clean PSModulePath; native stderr never aborts a step (exit codes decide).
# Requires: OpenSSH client (ssh/scp) with key auth to $SshHost (ssh alias vps-bd2026 in
# ~/.ssh/config; ubuntu@bd2026.cc has no key and fails with publickey), sudo on the VPS for
# the mirror step, python for the page renderer. Never embeds tokens. ASCII only.
[CmdletBinding()]
param(
  [string]$SshHost = "vps-bd2026",
  [string]$RemoteDownloads = "/home/ubuntu/yuntech/public/downloads/fleet",
  [string]$PublicBase = "https://bd2026.cc/downloads/fleet",
  [string]$MirrorDir = "/var/www/dl-mirror/downloads/fleet",
  [string]$Python = "python",
  [string]$StageDir = "",
  [string]$DistDir = "",
  [switch]$PackController,
  [switch]$PublishAgent,
  [switch]$Deploy,
  [switch]$BuildSetup,
  [switch]$OverwriteSigned,
  [switch]$RequireSigned,
  [switch]$NoMirror,
  [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
# --- PowerShell host guard (0.3.8) ------------------------------------------------
# Started from pwsh 7: re-run this script under Windows PowerShell 5.1 with PSModulePath removed,
# so 5.1 rebuilds its own module path. (pwsh's Modules dirs shadow Microsoft.PowerShell.Utility /
# .Security and Get-FileHash / Get-AuthenticodeSignature then fail to autoload.)
# Started as 5.1 by a pwsh parent: drop the pwsh-only entries before any cmdlet autoloads.
function ConvertTo-WinPSArgs([hashtable]$Bound) {
  $out = @()
  foreach ($k in $Bound.Keys) {
    $v = $Bound[$k]
    if ($v -is [System.Management.Automation.SwitchParameter]) { if ($v.IsPresent) { $out += "-$k" } }
    elseif ("$v" -ne '') { $out += "-$k"; $out += [string]$v }
  }
  return $out
}
if ($PSVersionTable.PSEdition -eq 'Core') {
  $winps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
  if (-not (Test-Path -LiteralPath $winps)) { throw "Windows PowerShell 5.1 not found at $winps" }
  Write-Host "[host] pwsh $($PSVersionTable.PSVersion) detected: re-running under Windows PowerShell 5.1"
  $savedModulePath = $env:PSModulePath
  Remove-Item Env:PSModulePath -ErrorAction SilentlyContinue
  try { & $winps -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath @(ConvertTo-WinPSArgs $PSBoundParameters) }
  finally { $env:PSModulePath = $savedModulePath }
  exit $LASTEXITCODE
}
if ($env:PSModulePath) {
  $env:PSModulePath = (@($env:PSModulePath -split ';') | Where-Object {
      $_ -and $_ -notmatch '\\PowerShell\\7|\\Documents\\PowerShell\\Modules|\\Program Files\\PowerShell\\Modules' }) -join ';'
}
# Native tools (ssh / scp / tar / python / ISCC) write progress and warnings to stderr. Under 5.1
# with ErrorActionPreference=Stop and redirected output that stderr turns into a terminating
# NativeCommandError, so native steps run with Continue and are judged by exit code only.
function Invoke-Native([scriptblock]$Block) {
  $ErrorActionPreference = 'Continue'
  & $Block
}
# ------------------------------------------------------------------------------------
function Say($m) { Write-Host "[publish] $m" }
function Run($cmd) { Say $cmd; if (-not $WhatIf) { Invoke-Native { Invoke-Expression $cmd }; if ($LASTEXITCODE -ne 0) { throw "failed: $cmd" } } }

$engine = Resolve-Path (Join-Path $PSScriptRoot "..\..")
# -PackController alone packs the controller and touches nothing under the public downloads.
# Uploading the agent as well needs -PublishAgent, so a controller deploy can never go out
# ahead of (or over) a release that is still being staged.
$doAgentUpload = (-not $PackController) -or $PublishAgent
if (-not $doAgentUpload) { Say "-PackController without -PublishAgent: agent files and the download mirror are NOT uploaded" }
if ($doAgentUpload) {
  if (-not $DistDir) { $DistDir = Join-Path $engine "fleet_agent\dist" }
  $mf = Join-Path $DistDir "manifest.json"
  if (-not (Test-Path $mf)) { throw "no $mf -- run: python fleet_agent\build_agent.py --base-url $PublicBase/" }
  $m = Get-Content $mf -Raw | ConvertFrom-Json
  Say "agent $($m.version) sha256=$($m.sha256.Substring(0,12))... file=$($m.file)"
  if ($m.url -notlike "$PublicBase/*") { Say "WARN manifest.url=$($m.url) does not start with $PublicBase (rebuild with --base-url)" }

  $setupScript = Join-Path $engine "fleet_agent\build_setup.ps1"
  $setupExe = Join-Path $DistDir "ChatXAgentSetup.exe"
  function SetupSignature([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return "Missing" }
    try { return [string](Get-AuthenticodeSignature -LiteralPath $path).Status } catch { return "Unknown" }
  }
  if ($BuildSetup) {
    $iscc = $env:INNO_SETUP
    if (-not $iscc) {
      foreach ($c in @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
      )) { if ($c -and (Test-Path -LiteralPath $c)) { $iscc = $c; break } }
    }
    if (-not $iscc) { throw "ISCC.exe not found. winget install --id JRSoftware.InnoSetup -e" }
    if (((SetupSignature $setupExe) -eq "Valid") -and -not $OverwriteSigned) {
      throw "refusing to rebuild over a signed $setupExe (pass -OverwriteSigned to replace it, then sign again)"
    }
    Say "building ChatXAgentSetup.exe (-BuildSetup)"
    $bsArgs = @('-NoProfile', '-File', $setupScript, '-DistDir', $DistDir)
    if ($OverwriteSigned) { $bsArgs += '-Force' }
    if (-not $WhatIf) { Invoke-Native { & powershell @bsArgs }; if ($LASTEXITCODE -ne 0) { throw "build_setup.ps1 failed" } }
    $m = Get-Content $mf -Raw | ConvertFrom-Json
  } else {
    Say "ChatXAgentSetup.exe is not rebuilt (no -BuildSetup); dist\ is uploaded as is"
  }
  # Pre-upload checks: the installer that goes out is the one the manifest pins.
  if (Test-Path -LiteralPath $setupExe) {
    $actual = (Get-FileHash -LiteralPath $setupExe -Algorithm SHA256).Hash.ToLower()
    if ($m.setup_sha256 -and ("$($m.setup_sha256)".ToLower() -ne $actual)) {
      throw "manifest.setup_sha256 does not match $setupExe ($actual); rebuild the manifest after signing"
    }
    $side = "$setupExe.sha256"
    if (Test-Path -LiteralPath $side) {
      $sideHash = ((Get-Content -LiteralPath $side -Raw) -split '\s+')[0].ToLower()
      if ($sideHash -ne $actual) { throw "$side does not match $setupExe" }
    }
    $sig = SetupSignature $setupExe
    Say "ChatXAgentSetup.exe sha256=$($actual.Substring(0,12))... signature=$sig"
    if ($RequireSigned -and $sig -ne "Valid") { throw "-RequireSigned: $setupExe signature is $sig" }
  } elseif ($RequireSigned) {
    throw "-RequireSigned: $setupExe is missing"
  }
  Run "ssh $SshHost 'mkdir -p $RemoteDownloads'"
  $names = @($m.file, "$($m.file).sha256", "manifest.json", "Install-ChatXAgent.ps1", "Uninstall-ChatXAgent.ps1", "ChatXAgentSetup.exe", "ChatXAgentSetup.exe.sha256")
  if ($m.setup_file) { $names += @([string]$m.setup_file, ([string]$m.setup_file + ".sha256")) }
  $files = $names | Select-Object -Unique | ForEach-Object { Join-Path $DistDir $_ } | Where-Object { Test-Path $_ }
  # versioned copy keeps old installers downloadable for rollback: chatx-agent-<ver>.exe
  Run ("scp " + (($files | ForEach-Object { '"' + $_ + '"' }) -join ' ') + " ${SshHost}:$RemoteDownloads/")
  Run "ssh $SshHost 'cp -f $RemoteDownloads/$($m.file) $RemoteDownloads/chatx-agent-$($m.version).exe && ls -la $RemoteDownloads'"

  # Mirror: nginx (snippets/fleet-downloads.conf) serves the versioned exes and the download page
  # straight from $MirrorDir. Next.js only sees public/ files that existed at its start.
  $mirrorNames = @()
  $ver = [string]$m.version
  if (-not $NoMirror) {
    if ($ver -notmatch '^[0-9][0-9.]*$') { throw "manifest.version '$ver' is not a plain version; the nginx mirror location would not match it" }
    if ($MirrorDir -notmatch '^/[A-Za-z0-9_./-]+$') { throw "MirrorDir '$MirrorDir' must be an absolute path without spaces" }
    $agentLocal = Join-Path $DistDir $m.file
    $agentVer = "chatx-agent-$ver.exe"
    $setupVer = "ChatXAgentSetup-$ver.exe"
    if (-not $StageDir) { $StageDir = Join-Path $env:TEMP "fleet-mirror-$ver" }
    if (Test-Path -LiteralPath $StageDir) { Remove-Item -LiteralPath $StageDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $remoteSrc = @{}
    if (Test-Path -LiteralPath $agentLocal) { $remoteSrc[$agentVer] = "$RemoteDownloads/$($m.file)" } else { throw "missing $agentLocal" }
    if (Test-Path -LiteralPath $setupExe) { $remoteSrc[$setupVer] = "$RemoteDownloads/ChatXAgentSetup.exe" }
    foreach ($n in @($agentVer, $setupVer)) {
      if (-not $remoteSrc.ContainsKey($n)) { continue }
      $local = $agentLocal; if ($n -eq $setupVer) { $local = $setupExe }
      $h = (Get-FileHash -LiteralPath $local -Algorithm SHA256).Hash.ToLower()
      [System.IO.File]::WriteAllText((Join-Path $StageDir "$n.sha256"), "$h  $n`n", $utf8)
      $mirrorNames += $n
    }
    $page = Join-Path $StageDir "index.html"
    if (Test-Path -LiteralPath $setupExe) {
      $built = ''
      if ($m.built_at -is [datetime]) { $built = $m.built_at.ToString('yyyy-MM-dd') }
      elseif ("$($m.built_at)" -match '^\d{4}-\d{2}-\d{2}') { $built = $Matches[0] }
      $renderer = Join-Path $PSScriptRoot "render_download_page.py"
      $pyArgs = @($renderer, '--version', $ver, '--setup', $setupExe, '--agent', $agentLocal, '--out', $page)
      if ($built) { $pyArgs += @('--date', $built) }
      # Local only, so it also runs under -WhatIf: the page can be reviewed before a real publish.
      Invoke-Native { & $Python @pyArgs }
      if ($LASTEXITCODE -ne 0) { throw "render_download_page.py failed" }
      Say "download page rendered from dist files: $page"
    } else {
      $page = ''
      Say "WARN no $setupExe : the public download page is not regenerated"
    }
    $ts = Get-Date -Format 'yyyyMMdd_HHmmss'
    $rstage = "/tmp/fleet-mirror-$ver-$ts"
    $stageFiles = @(Get-ChildItem -LiteralPath $StageDir -File | ForEach-Object { '"' + $_.FullName + '"' })
    Run "ssh $SshHost 'mkdir -p $rstage'"
    Run ("scp " + ($stageFiles -join ' ') + " ${SshHost}:$rstage/")
    $steps = @("sudo mkdir -p $MirrorDir")
    foreach ($n in $mirrorNames) {
      $srcPath = $remoteSrc[$n]
      # Versioned files are immutable (served with max-age): never swap the bytes behind a published name.
      $steps += "if [ -f $MirrorDir/$n ] && ! cmp -s $srcPath $MirrorDir/$n; then echo REFUSED_$n.differs_from_mirror; exit 4; fi"
      $steps += "sudo install -m 644 -o root -g root $srcPath $MirrorDir/$n"
      $steps += "sudo install -m 644 -o root -g root $rstage/$n.sha256 $MirrorDir/$n.sha256"
    }
    if ($page) {
      $steps += "if [ -f $MirrorDir/index.html ]; then sudo cp -p $MirrorDir/index.html $MirrorDir/index.html.bak_$ts; fi"
      $steps += "sudo install -m 644 -o root -g root $rstage/index.html $MirrorDir/index.html"
    }
    $steps += "cd $MirrorDir"
    $steps += "sha256sum -c " + (($mirrorNames | ForEach-Object { "$_.sha256" }) -join ' ')
    $steps += "rm -rf $rstage"
    Run ("ssh $SshHost '" + ($steps -join ' && ') + "'")
  } else {
    Say "WARN -NoMirror: $MirrorDir is not updated; versioned downloads and the download page stay stale"
  }

  if (-not $WhatIf) {
    try {
      $remote = Invoke-RestMethod -Uri "$PublicBase/manifest.json?t=$(Get-Date -UFormat %s)" -UseBasicParsing
      if ("$($remote.sha256)" -eq "$($m.sha256)") { Say "public manifest OK: $PublicBase/manifest.json -> $($remote.version)" }
      else { Say "WARN public manifest sha differs (CDN cache?)" }
    } catch { Say "WARN cannot fetch $PublicBase/manifest.json : $($_.Exception.Message)" }
    foreach ($n in $mirrorNames) {
      try {
        $r = Invoke-WebRequest -Uri "$PublicBase/$n" -Method Head -UseBasicParsing
        Say "public $n -> HTTP $($r.StatusCode)"
      } catch { Say "WARN public $PublicBase/$n : $($_.Exception.Message)" }
    }
    if ($mirrorNames.Count -gt 0) {
      try {
        $p = Invoke-WebRequest -Uri "$PublicBase/?t=$(Get-Date -UFormat %s)" -UseBasicParsing
        if ($p.Content -match ('name="fleet-download-version" content="' + [regex]::Escape($ver) + '"')) { Say "public download page OK: v$ver" }
        else { Say "WARN public download page does not show v$ver" }
      } catch { Say "WARN cannot fetch $PublicBase/ : $($_.Exception.Message)" }
    }
  }
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
      Invoke-Native { & tar -cf $raw --exclude=.venv --exclude=node_modules --exclude=desktop --exclude=tests --exclude=sessions `
          --exclude=config --exclude=logs --exclude=data --exclude=__pycache__ --exclude=fleet_agent/dist --exclude=fleet_agent/build --exclude=*.bak_* `
          --exclude=.git . }
      if ($LASTEXITCODE -ne 0) { throw "tar failed" }
      if (Test-Path (Join-Path $engine "config\presets")) {
        Invoke-Native { & tar -rf $raw --exclude=*.bak_* config/presets }
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
if ($doAgentUpload) {
  Say "next: update fleet_control.download.* in /etc/chatx-fleet/config.yaml (version=$($m.version) installer_url=$($m.installer) sha256=$($m.sha256)) or leave /fleet/ page to manifest.json"
}
