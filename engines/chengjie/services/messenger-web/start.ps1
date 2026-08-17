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
# 交互登录：0=headed（弹窗，运营在窗口内完成官方登录）；1=无头（一般不用，登录要人工过 2FA）
$env:MSG_HEADLESS = "0"
# restore/开机恢复/崩溃自愈已授权会话：1=无头后台保活（默认，稳态 0 可见窗口，多账号规模化
# 的窗口治理靠这条）；0=restore 也 headed（旧行为，调试用）。交互登录仍受 MSG_HEADLESS 管。
$env:MSG_RESTORE_HEADLESS = "1"
# 开机自动恢复持久化 profile（headed 默认关 → 曾出现「主进程先起时 restore 落空、
# 账号一直不上线」的启动顺序依赖）。显式开：服务一起来就恢复会话，与主进程启动顺序解耦。
$env:MSG_RESTORE_ON_BOOT = "1"
# 入站轮询间隔（毫秒）；0 关闭入站同步
$env:MSG_POLL_MS = "4000"
# 未读驱动强制读取（2026-08-15 P0，默认开）：E2EE 会话密钥未恢复时左栏预览永远是
# 加密占位 → 「预览变化」检测器对新消息全盲（实锤丢 2 条客户消息）。行级未读标记 /
# 全局未读+新鲜占位行 → 强制进线程读一次（共享 MSG_MAX_OPENS 预算、每线程 10min
# 冷却）。关闭：MSG_UNREAD_FORCE=0；细调 MSG_UNREAD_FORCE_COOLDOWN_MS /
# MSG_UNREAD_FORCE_CAP / MSG_UNREAD_FRESH_MS（默认值在 server.js）。
# 手发出站实时回流（2026-08-16，默认开）：运营在手机 App/原生网页手发的消息，
# 预览变成非自发的 "你:/You:" → 进线程镜像进统一收件箱（≈一个 poll tick 内可见；
# 旧行为要等客户回复才顺路回流、首条被水位基线吞掉＝坐席端隐形）。
# 关闭：MSG_MANUAL_OUT_SYNC=0；单轮镜像导航上限 MSG_MANUAL_OUT_CAP（默认 2，
# 追加在候选队尾，真实客户入站永远优先用 MSG_MAX_OPENS 预算）。
# 首连历史回填：登录后对最近 N 个会话各回填末尾若干条历史（0 关闭）。
# P1 起真实生效：每 tick 回填 1 条线程 → POST /api/internal/protocol/thread-history
# （Python 侧只对空会话落库=重启幂等；历史不触发 AI/SSE/未读）。
$env:MSG_BACKFILL = "20"
$env:MSG_SYNC = "1"
# P1 目录同步 / 拉更早（其余默认值在 server.js）：
#   MSG_DIR_SYNC_EVERY      会话占位推送周期（tick 数；server.js 默认 75≈5min）
#   MSG_DIR_SYNC_MIN_MS=30000 两次推送最小间隔（防抖）
# 2026-08-16 提速到 8 tick（≈32s）：对方改名/换头像要近实时反映到坐席端——目录推送
# 是唯一把「名字 + 头像直链」刷进库的链路（头像陈旧检测也吃这里的 avatar_url 指纹）。
# 指纹去重 + MIN_MS 防抖仍在：列表没变化时提频只是空转哈希比对，不产生推送/导航。
$env:MSG_DIR_SYNC_EVERY = "8"
#   MSG_BURST_MAX=6          一次变更最多补报的对端连发条数
#   MSG_INBOX_HEALTH_EVERY=15 入站健康心跳周期（tick 数，≈60s）
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
# 日志重定向交给 cmd 的 `>>`，由内核直接把 node 的 stdout 落盘。
# 不能用 PowerShell 管道逐行 Add-Content（曾用）：那样 PowerShell 是 stdout 的唯一消费者，
# 而它每行都要开关一次文件句柄，写盘速度远赶不上 node 产出 → 管道缓冲一满，node 侧的
# stdout 写入就转为**阻塞**，整个事件循环卡死（实测 /health 间歇超时、日志断流数小时，
# 服务却看着"活着"）。也不能用 `*>> $log`（PS5.1 按 UTF-16LE 落盘）或 `Out-File -Append`
# （全程独占句柄，服务在跑时读不了日志）。cmd 重定向没有中间消费者，且 node 输出本就是
# UTF-8 字节流，直写即得正确编码（顺带修掉此前的二次转码乱码）。
$cmdLine = '"' + $nodeExe + '" "' + $serverJs + '" >> "' + $log + '" 2>&1'
& cmd /c $cmdLine
