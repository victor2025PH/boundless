# chatx_gate.ps1 - operator entry for the ChatX AI-gateway eligibility gate (runs from 117).
#
# Wraps chatx_gate.sh: scps it to the VPS (fixing CRLF), runs it, prints the result. The gate
# governs BOTH AI-character and Telegram-credential white-listing via one env switch
# (AI_GATEWAY_REQUIRE_CLAIM). See chatx_gate.sh for the semantics.
#
#   powershell -File deploy\instances\chatx_gate.ps1                 # status + water-lines (read-only)
#   powershell -File deploy\instances\chatx_gate.ps1 -Close          # require claim (stops white-list)
#   powershell -File deploy\instances\chatx_gate.ps1 -Open           # install-and-use (production default)
#
# -Close/-Open restart the site's pm2 process (~4s public blip) and ask for confirmation unless -Yes.
param(
    [switch]$Open,
    [switch]$Close,
    [switch]$Yes,
    [string]$Key  = "$HOME\.ssh\hualing_deploy",
    [string]$Vps  = "ubuntu@165.154.233.121"
)
$ErrorActionPreference = "Stop"
$sh = Join-Path $PSScriptRoot "chatx_gate.sh"
if (-not (Test-Path $sh))  { Write-Error "missing $sh"; exit 1 }
if (-not (Test-Path $Key)) { Write-Error "missing ssh key $Key"; exit 1 }
if ($Open -and $Close)     { Write-Error "pick one of -Open / -Close"; exit 1 }

$action = if ($Close) { "on" } elseif ($Open) { "off" } else { "status" }

if ($action -ne "status" -and -not $Yes) {
    $what = if ($action -eq "on") { "CLOSE the gate (new installs must claim before AI/TG work)" }
            else { "OPEN the gate (install-and-use; anyone gets AI/TG)" }
    Write-Host "About to $what and restart the site (~4s blip)." -ForegroundColor Yellow
    $ans = Read-Host "Type YES to proceed"
    if ($ans -ne "YES") { Write-Host "aborted."; exit 0 }
}

$remote = "/tmp/chatx_gate.sh"
& scp -i $Key -o BatchMode=yes -o StrictHostKeyChecking=accept-new $sh "${Vps}:$remote" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "scp failed"; exit 1 }
# strip CRLF/BOM so remote bash is happy, then run with the chosen action
# -n (StdinNull): one-shot ssh from Windows 9.5p1 can hang forever after a fast remote
# command when stdin is unattended (Win32-OpenSSH #1334, 2026-09-11); stdin is unused here.
& ssh -n -i $Key -o BatchMode=yes -o StrictHostKeyChecking=accept-new $Vps `
    "sed -i '1s/^\xEF\xBB\xBF//;s/\r`$//' $remote && bash $remote $action; rm -f $remote"
exit $LASTEXITCODE
