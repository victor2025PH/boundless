# trial_rig.ps1 — 「注册领 7 天 + 加客服领 10 万字符」本地端到端测试台
#
# 为什么要台子：这条链跨三个系统（桌面壳 → 引擎后端 → 官网台账 → 厂商机签发），
# 单测各自都绿，但「人点一下会怎样」只有真跑起来才知道。本脚本把三方都拉起来，
# 且**与生产完全隔离**：
#
#   · 独立数据根 $Root（自己的 config / SQLite / license.key / trial_claim.json）
#   · 独立端口 18787（生产是智聊 18799 / 通译 18899，绝不相撞）
#   · Telegram 凭证留空（照抄 config.desktop.min.yaml 种子）——不会抢生产协议号会话
#   · 官网用本地 dev server + 独立 LEADS_DIR，不写真台账
#   · 签发用真厂商私钥（仓外离线保管），所以签出来的授权在任何构建里都验得过
#
# 用法：
#   scripts\trial_rig.ps1 -Up       # 拉起三方并等就绪，打印下一步操作
#   scripts\trial_rig.ps1 -Status   # 看三方在不在、单子进度到哪了
#   scripts\trial_rig.ps1 -Reset    # 清空领取状态与授权，让向导可以重来
#   scripts\trial_rig.ps1 -Down     # 全部停掉
[CmdletBinding()]
param(
  [switch]$Up,
  [switch]$Down,
  [switch]$Reset,
  [switch]$Status,
  [string]$Root = "D:\chengjie-instances\_trial_rig",
  [int]$Port = 18787,
  [int]$SitePort = 3571,
  [string]$Token = "admin",
  [string]$VendorKey = "D:\chengjie-instances\vendor\chengjie_vendor_private.pem",
  [string]$AdminKey = "rig-admin-key",
  [string]$ConsoleKey = "rig-console-key"
)

$ErrorActionPreference = "Stop"
$Engine = Split-Path -Parent $PSScriptRoot
$Website = Join-Path (Split-Path -Parent (Split-Path -Parent $Engine)) "website"
$RigDir = Join-Path $Root "_rig"
$PidFile = Join-Path $RigDir "pids.json"
$LeadsDir = Join-Path $Root "_leads"
$CfgDir = Join-Path $Root "config"

function Say($m, $c = "Gray") { Write-Host $m -ForegroundColor $c }
function Head($m) { Write-Host ""; Write-Host $m -ForegroundColor Cyan }

function Get-Pids {
  if (Test-Path $PidFile) { return Get-Content $PidFile -Raw | ConvertFrom-Json }
  return $null
}

function Test-Url($url, $timeoutSec = 3) {
  try {
    $r = Invoke-WebRequest -Uri $url -TimeoutSec $timeoutSec -UseBasicParsing -ErrorAction Stop
    return $r.StatusCode
  } catch {
    if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
    return 0
  }
}

# 「端口上有人应答」≠「那是我要的服务」。旧 dev server 的 ADMIN_KEY / LEADS_DIR 与本台
# 不同时，履约端取待办会 401 → 签发永远不发生，而三方看起来全是绿的。所以复用前必须
# 用本台密钥真打一次待办口（与后端 identity 探针同一个教训）。
function Test-SiteUsable {
  try {
    Invoke-RestMethod -Uri "http://localhost:$SitePort/api/admin/trial-claims?status=pending" `
      -Headers @{ "x-setup-key" = $AdminKey } -TimeoutSec 6 | Out-Null
    return $true
  } catch { return $false }
}

function Clear-Port($pt, $why) {
  $own = (Get-NetTCPConnection -LocalPort $pt -State Listen -ErrorAction SilentlyContinue).OwningProcess |
    Select-Object -Unique
  if (-not $own) {
    # Get-NetTCPConnection 偶尔看不到（IPv6/权限），退回 netstat 兜底
    $own = netstat -ano | Select-String "LISTENING" | Select-String ":$pt\s" |
      ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -Unique
  }
  foreach ($o in $own) {
    try {
      $nm = (Get-Process -Id $o -ErrorAction Stop).ProcessName
      Stop-Process -Id $o -Force
      Say "  已释放端口 $pt（pid $o $nm）——$why" Yellow
    } catch {}
  }
  if ($own) { Start-Sleep -Seconds 2 }
}

function Seed-Config {
  New-Item -ItemType Directory -Force -Path $CfgDir, $RigDir, $LeadsDir | Out-Null
  $target = Join-Path $CfgDir "config.yaml"
  if (-not (Test-Path $target)) {
    Copy-Item (Join-Path $Engine "config\config.desktop.min.yaml") $target
    Say "  已播种 config.yaml（照抄桌面最小种子：Telegram 留空）"
  }
  # overlay 里只放测试台专属的三件事，主配置保持种子原样
  $overlay = Join-Path $CfgDir "config.local.yaml"
  $yaml = @"
# 测试台专属 overlay（由 scripts\trial_rig.ps1 生成，可随时删）
licensing:
  trial:
    enabled: true
    enforce: false
    chars: 10000
    window_hours: 48
    # 指向本地官网 dev server，而不是 bd2026.cc
    site_url: http://localhost:$SitePort
    # 后台兜底轮询调快，方便观察「关掉向导也会自动到账」
    poll_interval_sec: 20
web_admin:
  enabled: true
  host: 127.0.0.1
  port: $Port
  auth_token: $Token
"@
  # 必须**不带 BOM**：PS5.1 的 `Set-Content -Encoding UTF8` 会写 BOM，PyYAML 读到
  # 行首 \ufeff 直接 scanner error → 整个 overlay 被静默丢掉 → site_url 回落成
  # 默认的 https://bd2026.cc，测试台就悄悄连上了生产台账（本脚本第一版真踩了）。
  [System.IO.File]::WriteAllText($overlay, $yaml, (New-Object System.Text.UTF8Encoding($false)))
  Say "  已写 config.local.yaml（无 BOM；site_url → localhost:$SitePort，兜底轮询 20s）"
}

# 测试台绝不能连生产台账。overlay 一旦没生效（BOM/缩进/路径任何原因），site_url 就会
# 回落成默认官网——而那种失败是静默的：三方全绿、单子却建到了真库里。所以就绪前用
# 后端同一套配置解析器把 site_url 读出来核对，不对就直接拒绝开台。
function Assert-SiteIsolated {
  $env:AITR_DATA_DIR = $Root
  $expect = "http://localhost:$SitePort"
  # 写成临时脚本再跑：内联 -c 要穿过 PowerShell/cmd/Python 三层引号，极易走形
  $probe = Join-Path $RigDir "_site_probe.py"
  @(
    "import asyncio, sys",
    "sys.path.insert(0, r'$Engine')",
    "from src.utils.config_manager import ConfigManager",
    "from src.licensing import trial_claim_client as tc",
    "cm = ConfigManager()",
    "asyncio.run(cm.load())   # load() 回 bool，配置在 cm.config 上",
    "print('SITEURL=' + tc.site_url(getattr(cm, 'config', None) or {}))"
  ) -join "`n" | Set-Content -Path $probe -Encoding ASCII
  # 配置自检会往 stderr 喊「Telegram api_id 无效」（本台故意留空，属预期）。
  # 脚本全局 ErrorActionPreference=Stop 会把原生 stderr 升级成终止错误，所以这里
  # 只收 stdout 并用哨兵行取值。
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  $lines = & python $probe 2>$null
  $ErrorActionPreference = $prev
  $hit = $lines | Where-Object { $_ -like "SITEURL=*" } | Select-Object -Last 1
  $got = if ($hit) { ($hit -replace '^SITEURL=', '').Trim() } else { "(取不到)" }
  if ($got -ne $expect) {
    Say "  site_url 实际解析为 '$got'，期望 '$expect'" Red
    Say "  overlay 没生效——拒绝开台（否则会把测试单子建到生产台账里）" Red
    return $false
  }
  Say "  site_url 已核对 = $got（未指向生产）" Green
  return $true
}

function Start-All {
  if (-not (Test-Path $VendorKey)) { throw "找不到厂商私钥: $VendorKey" }
  Head "[1/4] 准备隔离数据根 $Root"
  Seed-Config
  if (-not (Assert-SiteIsolated)) { return }

  $pids = @{}

  Head "[2/4] 启动官网 dev server (:$SitePort)"
  $siteAlive = (Test-Url "http://localhost:$SitePort/api/trial/claim-status?id=x") -ne 0
  if ($siteAlive -and (Test-SiteUsable)) {
    Say "  已在运行且接受本台密钥，复用" Yellow
  } else {
    if ($siteAlive) { Clear-Port $SitePort "该服务不接受本台 ADMIN_KEY，复用它会让签发静默失效" }
    $env:ADMIN_KEY = $AdminKey; $env:TELEGRAM_SETUP_KEY = $AdminKey
    $env:CONSOLE_KEY = $ConsoleKey; $env:LEADS_DIR = $LeadsDir
    $env:TRIAL_GIFT_CHARS = "100000"
    # npx 在 Windows 是 .cmd，Start-Process 直呼会 FileNotFound，必须过 cmd
    $p = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "npx next dev -p $SitePort") `
      -WorkingDirectory $Website -PassThru -WindowStyle Minimized `
      -RedirectStandardOutput (Join-Path $RigDir "site.log") `
      -RedirectStandardError (Join-Path $RigDir "site.err.log")
    $pids.site = $p.Id
    Say "  pid $($p.Id)，日志 $RigDir\site.log"
  }

  Head "[3/4] 启动隔离后端 (:$Port，数据根独立)"
  $beAlive = (Test-Url "http://127.0.0.1:$Port/login") -ne 0
  $beMine = $false
  if ($beAlive) {
    # 同理：18787 上那个后端可能是别的数据根起的（trial_claim.json 不在本台），
    # 直接复用会让「查看进度」看着一台、实际写另一台。用 identity 探针 + 端口归属判断。
    try {
      $ping = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/desktop/ping" -TimeoutSec 5
      $beMine = ($ping.app -eq "chengjie")
    } catch { $beMine = $false }
  }
  if ($beAlive -and $beMine) {
    Say "  已在运行（identity=chengjie），复用" Yellow
    Say "  注意：若它不是本台数据根起的，进度会写到别处——不确定就先 -Down" DarkGray
  } else {
    if ($beAlive) { Clear-Port $Port "端口被非 chengjie 后端占用" }
    $env:AITR_DATA_DIR = $Root
    $env:AITR_WEB_PORT = "$Port"
    $env:AITR_WEB_HOST = "127.0.0.1"
    $env:AITR_WEB_TOKEN = $Token
    $env:AITR_DESKTOP_MODE = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $p = Start-Process -FilePath "python" -ArgumentList @("main.py") `
      -WorkingDirectory $Engine -PassThru -WindowStyle Minimized `
      -RedirectStandardOutput (Join-Path $RigDir "backend.log") `
      -RedirectStandardError (Join-Path $RigDir "backend.err.log")
    $pids.backend = $p.Id
    Say "  pid $($p.Id)，日志 $RigDir\backend.log"
  }

  Head "[4/4] 启动厂商机履约守护（真私钥，15s 一轮）"
  $p = Start-Process -FilePath "python" `
    -ArgumentList @("scripts\fulfill_trial.py", "--site", "http://localhost:$SitePort",
      "--key", $AdminKey, "--priv", $VendorKey, "--interval", "15") `
    -WorkingDirectory $Engine -PassThru -WindowStyle Minimized `
    -RedirectStandardOutput (Join-Path $RigDir "fulfill.log") `
    -RedirectStandardError (Join-Path $RigDir "fulfill.err.log")
  $pids.fulfill = $p.Id
  Say "  pid $($p.Id)，日志 $RigDir\fulfill.log"

  $pids | ConvertTo-Json | Set-Content $PidFile -Encoding UTF8

  Head "等待三方就绪…"
  $deadline = (Get-Date).AddSeconds(150)
  $siteOk = $false; $beOk = $false
  while ((Get-Date) -lt $deadline -and -not ($siteOk -and $beOk)) {
    Start-Sleep -Seconds 3
    if (-not $siteOk) { $siteOk = (Test-Url "http://localhost:$SitePort/api/trial/claim-status?id=x") -ne 0 }
    if (-not $beOk) { $beOk = (Test-Url "http://127.0.0.1:$Port/login") -eq 200 }
    Say ("  官网 " + $(if ($siteOk) { "OK" } else { "…" }) + "   后端 " + $(if ($beOk) { "OK" } else { "…" }))
  }
  if (-not ($siteOk -and $beOk)) {
    Say "`n有一方没起来，看日志：$RigDir" Red
    Say "  官网 $RigDir\site.err.log ；后端 $RigDir\backend.err.log" Red
    return
  }

  Head "测试台就绪"
  Say "  官网台账   http://localhost:$SitePort   (ADMIN_KEY=$AdminKey CONSOLE_KEY=$ConsoleKey)"
  Say "  隔离后端   http://127.0.0.1:$Port       (令牌 $Token，数据根 $Root)"
  Say "  生产实例   18799 / 18899 未被触碰"
  Write-Host ""
  Say "下一步：桌面壳指向本测试台并重弹向导" White
  Say "  1) cd $Engine\desktop" DarkGray
  Say "  2) 确认 config.json 的 backend.base_url = http://127.0.0.1:$Port" DarkGray
  Say "  3) npm start" DarkGray
  Say "  4) 窗口里 Ctrl+Shift+I 打开 DevTools，Console 执行：" DarkGray
  Say "     localStorage.removeItem('aitr_firstrun_v1'); location.reload()" DarkGray
}

function Stop-All {
  $pids = Get-Pids
  if ($null -eq $pids) { Say "没有记录在案的进程（也可能是手动起的）" Yellow }
  else {
    foreach ($k in @("fulfill", "backend", "site")) {
      $id = $pids.$k
      if ($id) {
        try { Stop-Process -Id $id -Force -ErrorAction Stop; Say "已停 $k (pid $id)" }
        catch { Say "$k (pid $id) 已不在" DarkGray }
      }
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
  }
  # 端口兜底：Start-Process 起的 npx/python 可能有子进程
  foreach ($pt in @($Port, $SitePort)) {
    $own = (Get-NetTCPConnection -LocalPort $pt -State Listen -ErrorAction SilentlyContinue).OwningProcess |
      Select-Object -Unique
    foreach ($o in $own) {
      try { Stop-Process -Id $o -Force -ErrorAction Stop; Say "释放端口 $pt (pid $o)" } catch {}
    }
  }
}

function Reset-State {
  Head "清空领取状态（让首启向导可以重来）"
  foreach ($f in @("trial_claim.json", "license.key", "local_trial.json")) {
    $p = Join-Path $CfgDir $f
    if (Test-Path $p) { Remove-Item $p -Force; Say "  已删 $f" } else { Say "  $f 本来就没有" DarkGray }
  }
  if (Test-Path $LeadsDir) { Remove-Item $LeadsDir -Recurse -Force; Say "  已清官网测试台账" }
  Say "`n还需在桌面窗口 DevTools Console 执行（清掉「只弹一次」标记）：" White
  Say "  localStorage.removeItem('aitr_firstrun_v1'); location.reload()" DarkGray
  Say "后端若在跑，改完状态建议重启后端让单例重读：-Down 再 -Up" DarkGray
}

function Show-Status {
  Head "进程"
  $pids = Get-Pids
  foreach ($k in @("site", "backend", "fulfill")) {
    $id = if ($pids) { $pids.$k } else { $null }
    $alive = if ($id) { [bool](Get-Process -Id $id -ErrorAction SilentlyContinue) } else { $false }
    Say ("  {0,-9} {1}" -f $k, $(if ($alive) { "运行中 (pid $id)" } else { "未运行" }))
  }
  Head "端点"
  Say ("  官网 :{0}  HTTP {1}" -f $SitePort, (Test-Url "http://localhost:$SitePort/api/trial/claim-status?id=x"))
  Say ("  后端 :{0}  HTTP {1}" -f $Port, (Test-Url "http://127.0.0.1:$Port/login"))

  Head "本机领取进度（$CfgDir\trial_claim.json）"
  $st = Join-Path $CfgDir "trial_claim.json"
  if (Test-Path $st) { Get-Content $st -Raw } else { Say "  还没领过" DarkGray }

  Head "授权"
  $lk = Join-Path $CfgDir "license.key"
  if (Test-Path $lk) { Say "  license.key 已落盘（$((Get-Item $lk).Length) 字节）" Green }
  else { Say "  尚无 license.key（体验档在顶班）" DarkGray }

  Head "官网待办队列"
  try {
    $r = Invoke-RestMethod -Uri "http://localhost:$SitePort/api/admin/trial-claims?status=pending" `
      -Headers @{ "x-setup-key" = $AdminKey } -TimeoutSec 5
    Say ("  pending {0} 条；台账合计 {1}" -f $r.claims.Count, ($r.stats | ConvertTo-Json -Compress))
  } catch { Say "  取不到（官网没起？）" DarkGray }

  Head "履约日志尾部"
  $fl = Join-Path $RigDir "fulfill.log"
  if (Test-Path $fl) { Get-Content $fl -Tail 12 } else { Say "  无" DarkGray }
}

if ($Down) { Stop-All; return }
if ($Reset) { Reset-State; return }
if ($Status) { Show-Status; return }
if ($Up) { Start-All; return }

Say "用法：-Up | -Status | -Reset | -Down   （详见脚本头部注释）" Yellow
