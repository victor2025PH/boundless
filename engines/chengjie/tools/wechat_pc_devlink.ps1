# wechat_pc_devlink.ps1 — 个人微信 PC 副驾真机联调一键脚本（实施97 线 B）
# 用法（引擎根目录）：
#   powershell -ExecutionPolicy Bypass -File tools\wechat_pc_devlink.ps1                 # 探针 + 自检 + 起副驾服务
#   powershell -ExecutionPolicy Bypass -File tools\wechat_pc_devlink.ps1 -ProbeOnly      # 只跑探针 + 自检
#   powershell -ExecutionPolicy Bypass -File tools\wechat_pc_devlink.ps1 -Tier semi      # 半自动（只发人审通过的）
# 前置：PC 微信已由主人登录并保持窗口可见；联调后端已起（默认 D:\wxpc_dev 实例，端口 18898）。
# 安全：档位以 -ConfigFile 里 platform_login.wechat_pc 为准（引导页保存即热生效）；-Tier 只在显式给出时覆盖；
#       auto_reply 需同时给 -RiskAck（或配置里已 risk_ack）。永不把主号当测试号。
# 注意：本脚本是联调/无头部署入口；正常使用请在引导页点「启动副驾」（后端 supervisor 托管，无需命令行）。
[CmdletBinding()]
param(
    [string]$BackendUrl = 'http://127.0.0.1:18898',
    [string]$TokenFile  = 'D:\wxpc_dev\TOKEN.txt',
    [string]$AccountId  = 'wxpc-dev-1',
    [ValidateSet('', 'copilot', 'semi', 'auto_reply')]
    [string]$Tier       = '',
    [switch]$RiskAck,
    [switch]$ProbeOnly,
    [double]$Interval   = 3.0,
    [string]$ConfigFile = 'D:\wxpc_dev\data\config\config.local.yaml',   # 读 platform_login.wechat_pc（work_hours 等）
    [string]$StateDir   = 'D:\wxpc_dev\state',                           # 已读指纹 + 微信号↔显示名 落盘
    [double]$ConnectedDays = 30                                          # 该微信号在本机登录天数（预热期日配额）
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:PYTHONIOENCODING = 'utf-8'
$engine = Split-Path -Parent $PSScriptRoot
Set-Location $engine

if (-not (Get-Process -Name 'Weixin', 'WeChat' -ErrorAction SilentlyContinue)) {
    Write-Host '[devlink] 未检测到 PC 微信进程，请先启动并登录微信' -ForegroundColor Yellow
    exit 1
}
Write-Host '[devlink] ① 控件树探针（只读）→ logs\wechat_pc_control_tree.txt / logs\wechat_pc_probe.json'
python scripts\wechat_pc_probe.py --depth 18 --max-nodes 12000 --quiet
if ($LASTEXITCODE -ne 0) { Write-Host '[devlink] 探针未找到微信窗口' -ForegroundColor Red; exit 1 }
$summary = Get-Content logs\wechat_pc_probe.json -Raw | ConvertFrom-Json
foreach ($w in $summary.windows) {
    Write-Host ("[devlink]   窗口 {0} class={1} 节点={2} 有名={3} 列表项={4} 输入框={5} 登录窗={6}" -f `
        $w.window.name, $w.window.class, $w.node_count, $w.named_nodes, $w.list_items, $w.edit_boxes.Count, $w.login_window)
}
Write-Host '[devlink] ② 锚点自检（主窗必需锚点能否解析；缺则只读运行）'
python -m src.integrations.wechat_pc --self-check
$selfcheck = $LASTEXITCODE
if ($ProbeOnly) { exit $selfcheck }
if ($selfcheck -ne 0) {
    Write-Host '[devlink] 自检未通过（无主窗或未登录）——请登录微信后重跑' -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $TokenFile)) { Write-Host "[devlink] 找不到令牌文件 $TokenFile" -ForegroundColor Red; exit 1 }
$token = (Get-Content $TokenFile -Raw).Trim()
try {
    $ping = Invoke-RestMethod -Uri "$BackendUrl/api/desktop/ping" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 8
    Write-Host ("[devlink] ③ 后端就绪 {0} app={1}" -f $BackendUrl, $ping.app)
} catch {
    Write-Host "[devlink] 后端不可达 $BackendUrl ：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
# 令牌走文件路径而不是明文参数：同机其他用户 Get-Process 看不到令牌
$args_ = @('-u', '-m', 'src.integrations.wechat_pc', '--backend-url', $BackendUrl, '--token-file', $TokenFile,
           '--account-id', $AccountId, '--interval', "$Interval",
           '--state-dir', $StateDir, '--connected-days', "$ConnectedDays")
if ($Tier) { $args_ += @('--tier', $Tier) }
if (Test-Path $ConfigFile) { $args_ += @('--config', $ConfigFile) }
if ($RiskAck) { $args_ += '--risk-ack' }
$tierShown = if ($Tier) { $Tier } else { '(按配置文件，热生效)' }
Write-Host ("[devlink] ④ 启动副驾服务 tier={0} account={1}（Ctrl+C 停止；关闭本窗口副驾即停止）" -f $tierShown, $AccountId)
Write-Host ("[devlink]    工作台: {0}/workspace  （令牌见 {1}）" -f $BackendUrl, $TokenFile)
python @args_
