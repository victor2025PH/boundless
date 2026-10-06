# build_setup.ps1 -- compile ChatXAgentSetup.exe with the free Inno Setup 6 compiler.
#
#   powershell -File fleet_agent\build_setup.ps1
#
# Requires chatx-agent.exe already built (python fleet_agent\build_agent.py) and ISCC.exe.
# Install the compiler (no license fee):
#   winget install --id JRSoftware.InnoSetup -e
# or set INNO_SETUP to the full path of ISCC.exe.
# An existing dist\ChatXAgentSetup.exe with a valid Authenticode signature is not
# overwritten unless -Force is given (re-sign after a forced rebuild).
# After signing: powershell -File fleet_agent\build_setup.ps1 -ManifestOnly
#   re-hashes the existing (signed) installer into .sha256 and manifest.json, no ISCC.
# Started from pwsh 7 it re-runs itself under powershell.exe (clean PSModulePath).
# ASCII only.
[CmdletBinding()]
param(
  [string]$DistDir = "",
  [string]$Iscc = "",
  [switch]$Force,
  [switch]$ManifestOnly
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
$here = $PSScriptRoot
$engine = Resolve-Path (Join-Path $here "..")
if (-not $DistDir) { $DistDir = Join-Path $here "dist" }
$agent = Join-Path $DistDir "chatx-agent.exe"
$iss = Join-Path $here "setup\ChatXAgent.iss"
$setupDir = Join-Path $here "setup"
if (-not $ManifestOnly) {
if (-not (Test-Path -LiteralPath $agent)) { throw "missing $agent -- run: python fleet_agent\build_agent.py" }
if (-not (Test-Path -LiteralPath $iss)) { throw "missing $iss" }
$existingSetup = Join-Path $DistDir "ChatXAgentSetup.exe"
if ((Test-Path -LiteralPath $existingSetup) -and -not $Force) {
  $sigStatus = ""
  try { $sigStatus = [string](Get-AuthenticodeSignature -LiteralPath $existingSetup).Status } catch { $sigStatus = "" }
  if ($sigStatus -eq "Valid") {
    Write-Host "[setup] $existingSetup is signed; refusing to overwrite it. Re-run with -Force to rebuild (then sign again)."
    exit 3
  }
}


if (-not $Iscc) { $Iscc = $env:INNO_SETUP }
if (-not $Iscc -or -not (Test-Path -LiteralPath $Iscc)) {
  $candidates = @(
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
  )
  foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath $c)) { $Iscc = $c; break } }
}
if (-not $Iscc -or -not (Test-Path -LiteralPath $Iscc)) {
  Write-Host "Inno Setup 6 compiler (ISCC.exe) was not found."
  Write-Host "Install the free compiler, then re-run this script:"
  Write-Host "  winget install --id JRSoftware.InnoSetup -e"
  Write-Host "Or set INNO_SETUP to the full path of ISCC.exe."
  exit 2
}

$ver = "0.3.0"
$agentPy = Join-Path $engine "src\fleet\agent.py"
$hit = Select-String -Path $agentPy -Pattern 'AGENT_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($hit) { $ver = $hit.Matches[0].Groups[1].Value }

Write-Host "[setup] ISCC $Iscc  version $ver"
Invoke-Native { & $Iscc "/DAgentExe=$agent" "/DSetupDir=$setupDir" "/DDistDir=$DistDir" "/DAppVersion=$ver" $iss }
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
}

$setup = Join-Path $DistDir "ChatXAgentSetup.exe"
if (-not (Test-Path -LiteralPath $setup)) { throw "ISCC did not write $setup" }
$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLower()
Set-Content -LiteralPath ($setup + ".sha256") -Value "$hash  ChatXAgentSetup.exe" -Encoding ascii

$mfPath = Join-Path $DistDir "manifest.json"
if (Test-Path -LiteralPath $mfPath) {
  $mf = Get-Content -LiteralPath $mfPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $base = "https://bd2026.cc/downloads/fleet/"
  if ($mf.url) { $base = [string]$mf.url -replace '[^/]+$','' }
  $mf | Add-Member -NotePropertyName setup_file -NotePropertyValue "ChatXAgentSetup.exe" -Force
  $mf | Add-Member -NotePropertyName setup_url -NotePropertyValue ($base + "ChatXAgentSetup.exe") -Force
  $mf | Add-Member -NotePropertyName setup_sha256 -NotePropertyValue $hash -Force
  # PS 5.1 "-Encoding utf8" writes a BOM; controller/admin json.loads("utf-8") rejects it and /fleet/ shows the download as unpublished.
  [IO.File]::WriteAllText($mfPath, ($mf | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
  Write-Host "[setup] manifest setup_url=$base" "ChatXAgentSetup.exe"
}
Write-Host "[setup] $setup"
Write-Host "[setup] sha256 $hash"
