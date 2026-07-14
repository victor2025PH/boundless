# 用途：在部署或启动主控/Worker 前设置 OPENCLAW_BUILD_ID，便于 /health 与日志对照版本。
# 用法：在 PowerShell 中 dot-source： . .\scripts\set_build_id_env.ps1
# 或单行： $env:OPENCLAW_BUILD_ID = (git -C $PSScriptRoot\.. rev-parse --short HEAD 2>$null); if (-not $env:OPENCLAW_BUILD_ID) { $env:OPENCLAW_BUILD_ID = "unknown" }

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $short = ""
    try {
        $short = (& git rev-parse --short HEAD 2>$null).Trim()
    } catch { }
    if (-not $short) { $short = "unknown" }
    $env:OPENCLAW_BUILD_ID = $short
    Write-Host "OPENCLAW_BUILD_ID=$($env:OPENCLAW_BUILD_ID)"
} finally {
    Pop-Location
}
