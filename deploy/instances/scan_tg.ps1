# scan_tg.ps1 — 一键拉起「Telegram 总机扫码门户」（实施30：老板自助扫码接入）
#
# 背景：老板要把 Telegram 号扫码接入总机（智聊/通译实例）。旧法「扫了没反应」的根因是
#   静态过期二维码 + 无状态反馈。本封装启动 tg_scan_portal.py（自刷新码 + 实时状态 +
#   两步验证自助），并自动打开浏览器扫码页——老板点一下即可扫，无需记命令。
#
# 用法（默认接入智聊 zhiliao / 18799，门户 18790）：
#   powershell -ExecutionPolicy Bypass -File deploy\instances\scan_tg.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\instances\scan_tg.ps1 -Instance tongyi
#   powershell -ExecutionPolicy Bypass -File deploy\instances\scan_tg.ps1 -NoBrowser   # 不自动开浏览器
#
# 扫码：手机 Telegram → 设置 → 设备 → 连接桌面设备 → 扫弹出页里的二维码。
# 号开了两步验证时，页面会弹出云密码输入框，直接输入即可（密码不经过中转）。
# 扫成功后号自动写入账号注册表；若实例已开 orchestrator_enabled，重启/下一节拍即上线接待。

[CmdletBinding()]
param(
    [ValidateSet('zhiliao', 'tongyi')]
    [string]$Instance = 'zhiliao',
    [int]$PortalPort = 18790,
    [string]$DataRoot = '',          # 缺省 D:\chengjie-instances\<实例>\data
    [switch]$NoBrowser,
    [switch]$Stop                    # 停掉已在跑的门户（按门户端口/命令行匹配）
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$portMap = @{ zhiliao = 18799; tongyi = 18899 }
$backendPort = $portMap[$Instance]
if (-not $DataRoot) { $DataRoot = "D:\chengjie-instances\$Instance\data" }
$cfg = Join-Path $DataRoot 'config\config.local.yaml'
$portal = Join-Path $PSScriptRoot 'tg_scan_portal.py'
$base = "http://127.0.0.1:$backendPort"
$url = "http://127.0.0.1:$PortalPort/"

function Get-PortalPids {
    # 主：按门户端口取监听进程（最可靠）；仅当其命令行确为本门户脚本才认，防误杀他用
    $pids = @(Get-NetTCPConnection -LocalPort $PortalPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    @($pids | Where-Object {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$_" -ErrorAction SilentlyContinue
        # CommandLine 可能因权限为空 → 端口命中即认（门户端口非通用端口，误伤概率低）
        (-not $p) -or (-not $p.CommandLine) -or ($p.CommandLine -like '*tg_scan_portal.py*')
    })
}

if ($Stop) {
    $pids = Get-PortalPids
    if (-not $pids.Count) { Write-Host "[scan_tg] 门户（端口 $PortalPort）未在跑" -ForegroundColor Yellow; exit 0 }
    foreach ($pp in $pids) { Stop-Process -Id $pp -Force -ErrorAction SilentlyContinue; Write-Host "[scan_tg] 已停止门户 PID=$pp" -ForegroundColor Green }
    exit 0
}

# ── 防呆检查 ──────────────────────────────────────────────────────────────
if (-not (Test-Path $portal)) { Write-Host "[scan_tg] 错误: 门户脚本缺失 $portal" -ForegroundColor Red; exit 2 }
if (-not (Test-Path $cfg))    { Write-Host "[scan_tg] 错误: 实例配置缺失 $cfg（实例是否已初始化？）" -ForegroundColor Red; exit 2 }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Write-Host "[scan_tg] 错误: PATH 无 python" -ForegroundColor Red; exit 2 }

# 后端必须在跑且 protocol 通道就绪（否则门户空转出不了码）
try {
    $tokLine = (Get-Content $cfg | Select-String 'auth_token:' | Select-Object -First 1).Line
    $tok = ($tokLine -split 'auth_token:')[1].Trim().Trim('"').Trim("'")
} catch { $tok = '' }
if (-not $tok) { Write-Host "[scan_tg] 错误: 未能从 $cfg 读到 web_admin.auth_token" -ForegroundColor Red; exit 2 }
try {
    $modes = Invoke-RestMethod -Uri "$base/api/platforms/telegram/modes" -Headers @{ Authorization = "Bearer $tok" } -TimeoutSec 15
    $proto = @($modes.modes | Where-Object { $_.mode -eq 'protocol' })[0]
    if (-not $proto.available) {
        Write-Host "[scan_tg] 错误: 实例 $Instance 的 telegram protocol 通道未就绪（platform_login.telegram.protocol_enabled 是否为 true？）" -ForegroundColor Red
        exit 2
    }
} catch {
    Write-Host "[scan_tg] 错误: 后端 $base 不可达——先确认 $Instance 实例在跑（status_instances.ps1）: $($_.Exception.Message)" -ForegroundColor Red
    exit 2
}

# 门户就绪探测：轮询本地 /instances（v1/v2 门户都提供；比进程命令行匹配更可靠、规避启动时延）
function Test-PortalUp {
    try { Invoke-RestMethod -Uri ("http://127.0.0.1:$PortalPort/instances") -TimeoutSec 3 | Out-Null; return $true }
    catch { return $false }
}

# 已在跑则不重复起（幂等）——端口已有门户在服务即复用
if (Test-PortalUp) {
    Write-Host "[scan_tg] 门户已在跑（端口 $PortalPort），复用现有实例" -ForegroundColor Green
} else {
    $logDir = Join-Path $DataRoot 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $out = Join-Path $logDir 'portal.out.log'
    $err = Join-Path $logDir 'portal.err.log'
    $pyArgs = @($portal, '--base', $base, '--config', $cfg, '--portal-port', "$PortalPort", '--out-dir', $logDir)
    Start-Process -FilePath python -ArgumentList $pyArgs -WindowStyle Hidden `
        -RedirectStandardOutput $out -RedirectStandardError $err | Out-Null
    $ready = $false
    for ($i = 0; $i -lt 12; $i++) {   # 最多等 ~24s（python 启动 + 绑端口）
        Start-Sleep -Seconds 2
        if (Test-PortalUp) { $ready = $true; break }
    }
    if (-not $ready) {
        Write-Host "[scan_tg] 错误: 门户启动超时（端口 $PortalPort 未就绪），见 $err" -ForegroundColor Red
        exit 1
    }
    Write-Host "[scan_tg] 门户已启动（接入=$Instance 后端=$base）" -ForegroundColor Green
}

Write-Host ""
Write-Host "==================== Telegram 总机扫码接入 ====================" -ForegroundColor Cyan
Write-Host ("  1) 浏览器打开: {0}" -f $url) -ForegroundColor Cyan
Write-Host "  2) 手机 Telegram - 设置 - 设备 - 连接桌面设备 - 扫码" -ForegroundColor Cyan
Write-Host "  3) 若提示两步验证，在页面内输入云密码即可" -ForegroundColor Cyan
Write-Host "  停止门户: powershell -File deploy\instances\scan_tg.ps1 -Stop" -ForegroundColor DarkGray
Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host ""

if (-not $NoBrowser) { try { Start-Process $url } catch {} }
