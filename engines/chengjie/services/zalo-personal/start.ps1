# start.ps1 — 启动 Zalo 个人号（zca-js）扫码登录微服务
# 用法: pwsh -File start.ps1   (在 services/zalo-personal 下)
# 说明: 为主进程提供二维码登录 + 多账号保活；入站消息回推统一收件箱。
#       主进程需开 config.platform_login.zalo.web_enabled=true 并指向 zca_url。
# ⚠️ zca-js 是非官方 Zalo API（模拟 Zalo Web）：请用小号 + 独立代理，风险自负。

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# 服务监听端口（须与主进程 platform_login.zalo.zca_url 一致）
$env:PORT = "8792"
$env:BIND_HOST = "127.0.0.1"   # 入站无鉴权，只许本机引擎调；放开前先加鉴权（server.js 同注）
# 入站桥：收到的消息 push 进统一收件箱（web 后台 18799）
$env:PY_INGEST_URL = "http://127.0.0.1:18799/api/internal/protocol/ingest"
# 会话健康桥：连上/被登出/重连放弃等状态转移主动 push（不配则由 PY_INGEST_URL 自动推导）
$env:PY_STATUS_URL = "http://127.0.0.1:18799/api/internal/protocol/session-status"

# ingest endpoint 需 Bearer 鉴权（web_admin.auth_token）。多实例部署优先按 AITR_DATA_DIR
# 解析、overlay 优先于主配置（勿在本文件硬编码 token——本文件进 git）。
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
  Write-Host ("[zalo-personal] auth_token resolved from " + $cfgOverlay + " / " + $cfgMain)
} else {
  Write-Host "[zalo-personal] WARN: auth_token not found — inbound pushes may be 401-rejected"
}
$env:LOG_LEVEL = "info"

Write-Host "[zalo-personal] starting on :$($env:PORT) (ingest=$($env:PY_INGEST_URL))"
$logDir = Join-Path $root "services\zalo-personal\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("zalo-personal-" + (Get-Date -Format "yyyyMMdd") + ".log")
$serverJs = Join-Path $PSScriptRoot "server.js"
$nodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $nodeExe) {
  foreach ($cand in @("$env:ProgramFiles\nodejs\node.exe", "${env:ProgramFiles(x86)}\nodejs\node.exe", "$env:LOCALAPPDATA\Programs\nodejs\node.exe")) {
    if ($cand -and (Test-Path $cand)) { $nodeExe = $cand; break }
  }
}
Add-Content -LiteralPath $log -Value ("[zalo-personal] " + (Get-Date -Format o) + " launching node=" + ($(if ($nodeExe) { $nodeExe } else { "<NOT FOUND>" })) + " server=" + $serverJs)
if (-not $nodeExe) { Add-Content -LiteralPath $log -Value "[zalo-personal] FATAL: node.exe not found on PATH nor common install dirs"; exit 1 }
& $nodeExe $serverJs 2>&1 | ForEach-Object { Add-Content -LiteralPath $log -Value $_ -Encoding UTF8 }
