# build_setup.ps1 -- compile ChatXAgentSetup.exe with the free Inno Setup 6 compiler.
#
#   powershell -File fleet_agent\build_setup.ps1
#
# Requires chatx-agent.exe already built (python fleet_agent\build_agent.py) and ISCC.exe.
# Install the compiler (no license fee):
#   winget install --id JRSoftware.InnoSetup -e
# or set INNO_SETUP to the full path of ISCC.exe.
# ASCII only.
[CmdletBinding()]
param(
  [string]$DistDir = "",
  [string]$Iscc = ""
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$engine = Resolve-Path (Join-Path $here "..")
if (-not $DistDir) { $DistDir = Join-Path $here "dist" }
$agent = Join-Path $DistDir "chatx-agent.exe"
$iss = Join-Path $here "setup\ChatXAgent.iss"
$setupDir = Join-Path $here "setup"
if (-not (Test-Path -LiteralPath $agent)) { throw "missing $agent -- run: python fleet_agent\build_agent.py" }
if (-not (Test-Path -LiteralPath $iss)) { throw "missing $iss" }

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
& $Iscc "/DAgentExe=$agent" "/DSetupDir=$setupDir" "/DDistDir=$DistDir" "/DAppVersion=$ver" $iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }

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
  ($mf | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $mfPath -Encoding utf8
  Write-Host "[setup] manifest setup_url=$base" "ChatXAgentSetup.exe"
}
Write-Host "[setup] $setup"
Write-Host "[setup] sha256 $hash"
