# start.ps1 — 启动 WhatsApp (Baileys) 协议扫码登录微服务
# 用法: pwsh -File start.ps1   (在 services/whatsapp-baileys 下)
# 说明: 为主进程提供网页二维码登录 + 多账号连接；入站消息回推统一收件箱。
#       主进程需开 config.platform_login.whatsapp.protocol_enabled=true 并指向 baileys_url。

$ErrorActionPreference = "Stop"
# 定位到脚本自身目录：node server.js 用绝对路径启动，不再依赖调用方 CWD
# （计划任务/从别处调用时 CWD 不是本目录 → 原相对 `node server.js` 会 MODULE_NOT_FOUND）。
Set-Location -LiteralPath $PSScriptRoot
# chengjie 引擎根目录：从脚本位置派生（services/whatsapp-baileys → 上两级），
# 迁仓/换机后无需再改（原先硬编码 D:\workspace\telegram-mtproto-ai 已随单仓迁移失效）。
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# 服务监听端口（须与主进程 platform_login.whatsapp.baileys_url 一致）
$env:PORT = "8790"
# 入站桥：Baileys 收到的消息 push 进统一收件箱（web 后台 18799）
$env:PY_INGEST_URL = "http://127.0.0.1:18799/api/internal/protocol/ingest"
# 会话健康桥：连上/被登出/重连放弃等状态转移主动 push（不配则由 PY_INGEST_URL 自动推导）
$env:PY_STATUS_URL = "http://127.0.0.1:18799/api/internal/protocol/session-status"
# ingest endpoint requires Bearer auth (web_admin.auth_token). Must match the RUNNING
# instance's web_admin.auth_token or inbound pushes are 401-rejected and silently dropped.
# 多实例部署：真正在跑的实例把 config 放在 AITR_DATA_DIR\config（见 main 进程环境变量），
# 其 auth_token 在 overlay(config.local.yaml) 里且与仓库内 config.yaml 不同——所以优先按
# AITR_DATA_DIR 解析，overlay 优先于主配置；没有 AITR_DATA_DIR 才回落仓库 config。
# （勿在本文件硬编码 token——本文件进 git。）
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
  Write-Host ("[wa-baileys] auth_token resolved from " + $cfgOverlay + " / " + $cfgMain)
} else {
  Write-Host "[wa-baileys] WARN: auth_token not found — inbound pushes may be 401-rejected"
}
# 首连历史回填条数（0 关闭）
$env:WA_BACKFILL = "20"
# P0 同步开关：好友名单(通讯录) + 全量会话列表；会话占位上限防洪泛（默认开）
$env:WA_SYNC_CONTACTS = "1"
$env:WA_SYNC_CHATS = "1"
$env:WA_CHATS_MAX = "500"
# P2 群聊接入（入站落「群组动态」；置 0 回到只私聊）
$env:WA_SYNC_GROUPS = "1"
# P4-3/P4-4/P4-5A/P4-6A 消息级富交互：表情回应 + 已读回执 + 对端输入状态 + 编辑/撤回（置 0 关闭）
$env:WA_SYNC_REACTIONS = "1"
$env:WA_SYNC_RECEIPTS = "1"
$env:WA_SYNC_PRESENCE = "1"
$env:WA_SYNC_EDITS = "1"
# 媒体落地目录：必须与 Python 侧 protocol_bridge.protocol_media_root() 同址，否则
# 「边车写 A、引擎读 B」＝入站媒体后端识别链全断（ASR/图片 VLM/视频/OCR/声纹/贴纸），
# 而 UI 因 ProtocolMediaStatic 双根兜底照样能播 → 症状只表现为「AI 突然不回话」，
# 极难查。2026-08-20 实锤：客户语音落旧引擎树、ASR 在数据根找不到 → 无兜底纪律拦下
# 整条回复（delivery_block asr:enrich_failed）。
# 故解析顺序与 protocol_media_root() 逐字对齐：AITR_DATA_DIR（计划任务经
# autostart-run.ps1 注入，与上面 auth_token 同一契约）→ 无则回落旧引擎树 static
# （裸开发机：那边 Python 也回落同一处，仍然同址）。
if ($env:AITR_DATA_DIR) {
  $mediaRoot = Join-Path $env:AITR_DATA_DIR "protocol_media"
} else {
  $mediaRoot = Join-Path $root "src\web\static\protocol_media"
}
$env:WA_MEDIA_DIR = Join-Path $mediaRoot "whatsapp"
$env:WA_MEDIA_URL_BASE = "/static/protocol_media/whatsapp"
New-Item -ItemType Directory -Force -Path $env:WA_MEDIA_DIR | Out-Null
Write-Host ("[wa-baileys] media dir = " + $env:WA_MEDIA_DIR)
$env:LOG_LEVEL = "info"

Write-Host "[wa-baileys] starting on :$($env:PORT) (ingest=$($env:PY_INGEST_URL))"
# 日志落文件（隐藏窗口的计划任务下 stdout 会丢失，落盘便于事后排查连接/登出等问题）
$logDir = Join-Path $root "services\whatsapp-baileys\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("wa-baileys-" + (Get-Date -Format "yyyyMMdd") + ".log")
# 绝对路径起服务：CWD 不确定时（计划任务/-Command）也能找到 server.js（防 MODULE_NOT_FOUND）。
$serverJs = Join-Path $PSScriptRoot "server.js"
# node 绝对路径：计划任务/服务上下文的 PATH 可能不含 C:\Program Files\nodejs → 裸 `node` 会
# CommandNotFound、脚本 Stop 退出（正是本次计划任务失败 0xFFFFFFFF 的根因）。用 Get-Command
# 解析，取不到再回落常见安装位，仍无则落一条清晰错误到日志。
$nodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $nodeExe) {
  foreach ($cand in @("$env:ProgramFiles\nodejs\node.exe", "${env:ProgramFiles(x86)}\nodejs\node.exe", "$env:LOCALAPPDATA\Programs\nodejs\node.exe")) {
    if ($cand -and (Test-Path $cand)) { $nodeExe = $cand; break }
  }
}
Add-Content -LiteralPath $log -Value ("[wa-baileys] " + (Get-Date -Format o) + " launching node=" + ($(if ($nodeExe) { $nodeExe } else { "<NOT FOUND>" })) + " server=" + $serverJs)
if (-not $nodeExe) { Add-Content -LiteralPath $log -Value "[wa-baileys] FATAL: node.exe not found on PATH nor common install dirs"; exit 1 }
# 日志编码修复（P2）：PowerShell 5.1 下 `*>> $log` 会把 node stdout 按 UTF-16LE 落盘，
# 与上面 Add-Content 写入的单字节行混在同一文件 → 乱码且排障工具读不了（2026-07-22
# 事故排查即受害于此）。不能用 `Out-File -Append`：它全程独占文件句柄，服务在跑时
# 任何人都读不了日志（排障反而更瞎）。用逐行 Add-Content（UTF-8）：每行写完即释放
# 句柄，tail/Get-Content 随时可读；Baileys info 级日志量小，逐行开销可忽略。
& $nodeExe $serverJs 2>&1 | ForEach-Object { Add-Content -LiteralPath $log -Value $_ -Encoding UTF8 }
