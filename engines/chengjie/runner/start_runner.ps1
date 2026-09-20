# start_runner.ps1 -- launch the Xiaozhi Windows control runner (impl91).
# ASCII-only on purpose (PS 5.1 GBK console can mangle non-ASCII -> see runbook).
#
# SAFE BY DEFAULT: actions OFF, shell OFF, bind 127.0.0.1. Pass -Actions / -Shell
# to opt in per the staged rollout (see: python -m tools.pc_runner_preflight).
# Token is REQUIRED (runner refuses to start naked): -Token, or -TokenFile, or
# env PC_RUNNER_TOKEN. NEVER hardcode a token in this script.
#
# Run it under a LOW-PRIVILEGE account with an interactive desktop session
# (windows can't be enumerated from a headless/no-session context).
#
# Examples:
#   powershell -ExecutionPolicy Bypass -File runner\start_runner.ps1 -Token "<secret>"
#   powershell -ExecutionPolicy Bypass -File runner\start_runner.ps1 -Actions        # stage 2 (UI actions)
#   powershell -ExecutionPolicy Bypass -File runner\start_runner.ps1 -Actions -Shell # stage 4 (also run_command)

param(
    [string]$Token = "",
    [string]$TokenFile = "",
    [string]$Bind = "127.0.0.1",
    [int]$Port = 18760,
    [switch]$Actions,
    [switch]$Shell,
    [string]$KillFile = "",
    [string]$ShotsDir = ""
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$py = Join-Path $here "pc_runner.py"
if (-not (Test-Path $py)) { Write-Error "pc_runner.py not found next to this script"; exit 1 }

# Resolve token: -Token > -TokenFile > default token file > env. Never invent one.
if (-not $Token) {
    if (-not $TokenFile) { $TokenFile = Join-Path $here ".pc_runner_token" }
    if (Test-Path $TokenFile) { $Token = (Get-Content -Raw $TokenFile).Trim() }
}
if (-not $Token) { $Token = $env:PC_RUNNER_TOKEN }
if (-not $Token) {
    Write-Error ("PC_RUNNER_TOKEN missing. Provide -Token <secret>, or put it in " +
        "runner\.pc_runner_token (gitignored), or set env PC_RUNNER_TOKEN. It MUST " +
        "match the server side pc_runner.token / per-machine token.")
    exit 2
}

if (-not $KillFile) { $KillFile = Join-Path $here ".pc_runner_kill" }
if (-not $ShotsDir) { $ShotsDir = Join-Path $here "pc_runner_shots" }

# Kill-switch is presence-based: if the file EXISTS, actions are frozen. Start
# clean (remove a stale kill file) so a fresh launch isn't silently frozen.
if (Test-Path $KillFile) { Remove-Item $KillFile -Force -ErrorAction SilentlyContinue }

$env:PC_RUNNER_TOKEN = $Token
$env:PC_RUNNER_BIND = $Bind
$env:PC_RUNNER_PORT = "$Port"
$env:PC_RUNNER_KILL = $KillFile
$env:PC_RUNNER_SHOTS = $ShotsDir
# Opt-in capabilities (default OFF). Clear any inherited value so switches are authoritative.
if ($Actions) { $env:PC_RUNNER_ACTIONS = "1" } else { Remove-Item Env:\PC_RUNNER_ACTIONS -ErrorAction SilentlyContinue }
if ($Shell)   { $env:PC_RUNNER_SHELL   = "1" } else { Remove-Item Env:\PC_RUNNER_SHELL   -ErrorAction SilentlyContinue }

Write-Host "[start_runner] bind=$Bind port=$Port actions=$([bool]$Actions) shell=$([bool]$Shell)"
Write-Host "[start_runner] kill-switch file: $KillFile  (create it to freeze all actions)"
Write-Host "[start_runner] health: http://$Bind`:$Port/health"
if ($Shell -and -not $Actions) {
    Write-Host "[start_runner] NOTE: -Shell without -Actions has no effect (run_command is under the actions gate too)."
}

python $py
