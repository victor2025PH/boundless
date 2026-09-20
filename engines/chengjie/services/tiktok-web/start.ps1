# start.ps1 — 启动 TikTok 个人号网页托管登录 + 只读收件箱边车（Playwright；TK-3 ②-A 阶段 1 assistOnly）
# 用法: pwsh -File start.ps1   (在 services/tiktok-web 下)
# 说明: 真机（huoke）路不可用时的备用读路——把个人号私信读进智聊收件箱，不代发。
#       主进程需开 config.platform_login.tiktok.web_enabled=true 并指向 web_url（默认 http://127.0.0.1:8794）。
# ⚠️ 非官方接入（依赖 tiktok.com DOM）：小号 + 独立代理，风险自负；选择器须先按 README 清单用真号核对。

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

$env:PORT = "8794"
$env:BIND_HOST = "127.0.0.1"
$env:PY_INGEST_URL = "http://127.0.0.1:18799/api/internal/protocol/ingest"
$env:PY_STATUS_URL = "http://127.0.0.1:18799/api/internal/protocol/session-status"

if ($env:AITR_DATA_DIR) {
  $cfgMain = Join-Path $env:AITR_DATA_DIR "config\config.yaml"
  $cfgOverlay = Join-Path $env:AITR_DATA_DIR "config\config.local.yaml"
} else {
  $cfgMain = Join-Path $root "config\config.yaml"
  $cfgOverlay = Join-Path $root "config\config.local.yaml"
}
function Get-CfgToken([string[]]$files) {
  foreach ($f in $files) {
    if (Test-Path $f) {
      $m = Select-String -Path $f -Pattern '^\s*auth_token:\s*(\S+)' | Select-Object -Last 1
      if ($m) { return $m.Matches[0].Groups[1].Value }
    }
  }
  return ""
}
$env:PY_API_TOKEN = Get-CfgToken @($cfgOverlay, $cfgMain)
if ($env:PY_API_TOKEN) {
  Write-Host ("[tiktok-web] auth_token resolved from " + $cfgOverlay + " / " + $cfgMain)
} else {
  Write-Host "[tiktok-web] WARN: auth_token not found — inbound pushes may be 401-rejected"
}
$env:TT_HEADLESS = "0"
$env:TT_RESTORE_HEADLESS = "1"
$env:LOG_LEVEL = "info"

Write-Host "[tiktok-web] starting on :$($env:PORT) assist-only (ingest=$($env:PY_INGEST_URL))"
$logDir = Join-Path $root "services\tiktok-web\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("tiktok-web-" + (Get-Date -Format "yyyyMMdd") + ".log")
$serverJs = Join-Path $PSScriptRoot "server.js"
$nodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $nodeExe) {
  foreach ($cand in @("$env:ProgramFiles\nodejs\node.exe", "${env:ProgramFiles(x86)}\nodejs\node.exe", "$env:LOCALAPPDATA\Programs\nodejs\node.exe")) {
    if ($cand -and (Test-Path $cand)) { $nodeExe = $cand; break }
  }
}
Add-Content -LiteralPath $log -Value ("[tiktok-web] " + (Get-Date -Format o) + " launching node=" + ($(if ($nodeExe) { $nodeExe } else { "<NOT FOUND>" })) + " server=" + $serverJs)
if (-not $nodeExe) { Add-Content -LiteralPath $log -Value "[tiktok-web] FATAL: node.exe not found"; exit 1 }
& $nodeExe $serverJs 2>&1 | ForEach-Object { Add-Content -LiteralPath $log -Value $_ -Encoding UTF8 }
