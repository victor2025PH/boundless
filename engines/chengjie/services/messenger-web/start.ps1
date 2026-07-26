# start.ps1 — 启动 Messenger 网页模式（隔离浏览器 + 官方 messenger.com）登录微服务
# 用法: pwsh -File start.ps1   (在 services/messenger-web 下)
# 说明: 用 Playwright 驱动持久化 Chromium 加载 messenger.com，功能对齐官方网页版；
#       登录默认 headed（弹窗内完成官方扫码/账密/2FA），成功后收发经 DOM 自动化。
#       主进程需开 config.platform_login.messenger.web_enabled=true 并指向 web_url。

$ErrorActionPreference = "Stop"
# 定位到脚本自身目录：node server.js 用绝对路径启动，不再依赖调用方 CWD
# （计划任务/从别处调用时 CWD 不是本目录 → 相对 `node server.js` 会 MODULE_NOT_FOUND）。
Set-Location -LiteralPath $PSScriptRoot
# chengjie 引擎根目录：从脚本位置派生（services/messenger-web → 上两级）。
# 原先硬编码 D:\workspace\telegram-mtproto-ai 已随单仓迁移失效——该目录不存在，
# 在 Stop 模式下读 config 那步直接抛错退出，服务从未真正起来过。
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# 服务监听端口（须与主进程 platform_login.messenger.web_url 一致）
$env:PORT = "8791"
# 入站桥：Messenger 收到的消息 push 进统一收件箱（web 后台 18799）
$env:PY_INGEST_URL = "http://127.0.0.1:18799/api/internal/protocol/ingest"
# 会话健康桥（P0-2）：登录/掉线/放弃自愈等状态转移主动 push（不配则由 PY_INGEST_URL 自动推导）
$env:PY_STATUS_URL = "http://127.0.0.1:18799/api/internal/protocol/session-status"
# ingest endpoint 需 Bearer 鉴权，且必须匹配**真正在跑的那个实例**的 web_admin.auth_token，
# 否则入站被 401 静默丢弃。多实例部署下实例的配置在 AITR_DATA_DIR\config（overlay 优先于
# 主配置），与仓库内 config.yaml 不同 → 优先按 AITR_DATA_DIR 解析，没有才回落仓库配置。
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
  Write-Host ("[messenger-web] auth_token resolved from " + $cfgOverlay + " / " + $cfgMain)
} else {
  Write-Host "[messenger-web] WARN: auth_token not found - inbound pushes may be 401-rejected"
}
# 登录交互：0=headed（弹窗，运营在窗口内完成官方登录）；登录成功持久化后可切 1 后台常驻
$env:MSG_HEADLESS = "0"
# 开机自动恢复持久化 profile（headed 默认关 → 曾出现「主进程先起时 restore 落空、
# 账号一直不上线」的启动顺序依赖）。显式开：服务一起来就恢复会话，与主进程启动顺序解耦。
$env:MSG_RESTORE_ON_BOOT = "1"
# 入站轮询间隔（毫秒）；0 关闭入站同步
$env:MSG_POLL_MS = "4000"
# 首连回填最近会话末条数（0 关闭）
$env:MSG_BACKFILL = "20"
$env:MSG_SYNC = "1"
# 入站媒体落地目录（对齐 whatsapp-baileys）：进线程读到图片/视频等媒体气泡时，用浏览器会话
# 下载写入 Python 静态目录（同机共享），前端按 /static URL 加载。未配置则回落占位文本。
$env:MSG_MEDIA_DIR = "$root\src\web\static\protocol_media\messenger"
$env:MSG_MEDIA_URL_BASE = "/static/protocol_media/messenger"
$env:LOG_LEVEL = "info"

Write-Host "[messenger-web] starting on :$($env:PORT) (ingest=$($env:PY_INGEST_URL))"
# 日志落文件（隐藏窗口的计划任务下 stdout 会丢失，落盘便于事后排查登录/掉线问题）
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("messenger-web-" + (Get-Date -Format "yyyyMMdd") + ".log")
# 绝对路径起服务：CWD 不确定时（计划任务/-Command）也能找到 server.js（防 MODULE_NOT_FOUND）。
$serverJs = Join-Path $PSScriptRoot "server.js"
# node 绝对路径：计划任务/服务上下文的 PATH 可能不含 C:\Program Files\nodejs → 裸 `node` 会
# CommandNotFound、脚本 Stop 退出。用 Get-Command 解析，取不到再回落常见安装位。
$nodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $nodeExe) {
  foreach ($cand in @("$env:ProgramFiles\nodejs\node.exe", "${env:ProgramFiles(x86)}\nodejs\node.exe", "$env:LOCALAPPDATA\Programs\nodejs\node.exe")) {
    if ($cand -and (Test-Path $cand)) { $nodeExe = $cand; break }
  }
}
Add-Content -LiteralPath $log -Value ("[messenger-web] " + (Get-Date -Format o) + " launching node=" + ($(if ($nodeExe) { $nodeExe } else { "<NOT FOUND>" })) + " server=" + $serverJs)
if (-not $nodeExe) { Add-Content -LiteralPath $log -Value "[messenger-web] FATAL: node.exe not found on PATH nor common install dirs"; exit 1 }
# 日志编码修复：PowerShell 5.1 下 `*>> $log` 会把 node stdout 按 UTF-16LE 落盘，与上面
# Add-Content 写入的单字节行混在同一文件 → 乱码且排障工具读不了。不能用 `Out-File -Append`：
# 它全程独占文件句柄，服务在跑时谁都读不了日志。用逐行 Add-Content（UTF-8）：每行写完即释放
# 句柄，tail/Get-Content 随时可读。
& $nodeExe $serverJs 2>&1 | ForEach-Object { Add-Content -LiteralPath $log -Value $_ -Encoding UTF8 }
