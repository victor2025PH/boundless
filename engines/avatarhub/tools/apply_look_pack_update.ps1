<#
  apply_look_pack_update.ps1 — Look Pack 定妆包一键激活（2026-07-08）
  =====================================================================
  做什么（对应 妆容定妆包实施记录_20260708.md 的两步激活）：
    1) 直播避让闸：realtime_status.json 心跳 <30s 判定直播中 → 拒绝执行（-Force 可越，不建议）
    2) 重启本机 Hub(9000) 载入新代码 → 等健康 → 验证 /api/makeup/styles 新端点
    3) 部署 faceswap_api.py 到 192.168.0.104（scp→备份→py_compile 闸门→杀8000→计划任务重启）
       → 等健康 → openapi 验证 makeup 字段；失败自动回滚备份并重启
    4) 冒烟：临时角色 _lookpack_smoke 跑一次 makeup_preset（8004 在线时）→ 验证后删除
  用法（项目根 PowerShell）：
    powershell -ExecutionPolicy Bypass -File tools\apply_look_pack_update.ps1
    ... -SkipRemote   # 只激活本机 Hub（.104 不动）
    ... -Force        # 越过直播闸（会造成数秒换脸中断，慎用）
#>
[CmdletBinding()]
param(
  [switch]$Force,
  [switch]$SkipRemote,
  [string]$RemoteHost = '192.168.0.104',
  [string]$User = 'Administrator'
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$Base = Split-Path $PSScriptRoot -Parent
Set-Location $Base
$LogF = Join-Path $Base ("logs\look_pack_activate_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
Start-Transcript -Path $LogF | Out-Null

function Step([string]$m) { Write-Host "`n== $m ==" -ForegroundColor Cyan }
function Ok([string]$m) { Write-Host "  [OK] $m" -ForegroundColor Green }
function Die([string]$m) { Write-Host "  [FAIL] $m" -ForegroundColor Red; Stop-Transcript | Out-Null; exit 1 }

$Py = "$env:USERPROFILE\Miniconda3\envs\facefusion\python.exe"
if (-not (Test-Path $Py)) { $Py = 'python' }

function Wait-Health([string]$url, [int]$secs) {
  $t0 = Get-Date
  while (((Get-Date) - $t0).TotalSeconds -lt $secs) {
    try {
      $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 4
      if ($r.StatusCode -eq 200) { return $true }
    } catch {}
    Start-Sleep 3
  }
  return $false
}

# ---- 1) 直播避让闸 -------------------------------------------------------
Step '直播避让闸'
$st = Join-Path $Base 'realtime_status.json'
if (Test-Path $st) {
  try {
    $j = Get-Content $st -Raw -Encoding UTF8 | ConvertFrom-Json
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$j.ts
    if ($age -lt 30 -and -not $Force) {
      Die "检测到直播链路活跃（状态心跳 ${age}s 前）。请下播后再跑，或明知后果用 -Force。"
    }
    Ok "心跳 ${age}s 前$(if ($age -lt 30) {'（-Force 越闸执行）'} else {'，非直播状态'})"
  } catch { Ok "状态文件无法解析，视为非直播" }
} else { Ok "无状态文件，视为非直播" }

# ---- 2) 本机 Hub 重启载新码 ----------------------------------------------
Step '重启本机 Hub(9000)'
& $Py -m py_compile (Join-Path $Base 'avatar_hub.py')
if ($LASTEXITCODE -ne 0) { Die 'avatar_hub.py 编译不过，拒绝重启（改动有语法错误？）' }
Ok 'avatar_hub.py py_compile 通过'
$owners = Get-NetTCPConnection -LocalPort 9000 -State Listen -ErrorAction SilentlyContinue |
          Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $owners) { if ($p) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } }
Ok "旧 Hub 进程已停 ($($owners -join ','))"
Start-Sleep 12    # start_avatar_hub.bat 守护循环会自动重拉；等它先试
$relisten = Get-NetTCPConnection -LocalPort 9000 -State Listen -ErrorAction SilentlyContinue
if (-not $relisten) {
  Write-Host '  守护循环未接管，手动拉起 start_avatar_hub.bat ...'
  Start-Process cmd -ArgumentList '/c', (Join-Path $Base 'start_avatar_hub.bat') -WindowStyle Minimized
}
if (-not (Wait-Health 'http://127.0.0.1:9000/health' 90)) { Die 'Hub 90s 未就绪，看 logs\avatar_hub.log' }
Ok 'Hub 健康'
try {
  $ms = Invoke-RestMethod 'http://127.0.0.1:9000/api/makeup/styles' -TimeoutSec 6
  if ($ms.ok) { Ok "新端点 /api/makeup/styles 生效（妆容服务在线: $($ms.up)）" }
  else { Die '/api/makeup/styles 返回异常' }
} catch { Die "新端点未生效：$_（重启加载的还是旧代码？）" }
try {
  $tc = Invoke-RestMethod 'http://127.0.0.1:9000/api/tryon/clothes' -TimeoutSec 8
  if ($tc.ok) { Ok "试衣间代理 /api/tryon/clothes 生效（tryon 在线: $($tc.up) 后端: $($tc.backend)）" }
  else { Write-Host '  [WARN] /api/tryon/clothes 返回异常（试衣间面板可能不可用）' -ForegroundColor Yellow }
} catch { Write-Host "  [WARN] 试衣间代理未生效：$_" -ForegroundColor Yellow }

# ---- 3) .104 换脸机部署 ---------------------------------------------------
if (-not $SkipRemote) {
  Step "部署 faceswap_api.py → $RemoteHost"
  & $Py -m py_compile (Join-Path $Base 'faceswap_api.py')
  if ($LASTEXITCODE -ne 0) { Die 'faceswap_api.py 编译不过，拒绝分发' }
  scp -o ConnectTimeout=10 (Join-Path $Base 'faceswap_api.py') "$User@${RemoteHost}:/faceswap_api.py"
  if ($LASTEXITCODE -ne 0) { Die "scp 到 $RemoteHost 失败（OpenSSH 未通？）" }
  Ok 'scp 完成（ASCII 暂存 /faceswap_api.py）'
  $remote = @'
$ErrorActionPreference='SilentlyContinue'
$b='C:\模仿音色'; $py='C:\Users\Administrator\Miniconda3\envs\facefusion\python.exe'
Copy-Item "$b\faceswap_api.py" "$b\faceswap_api.py.bak_lookpack" -Force
Copy-Item 'C:\faceswap_api.py' "$b\faceswap_api.py" -Force
& $py -m py_compile "$b\faceswap_api.py"
if ($LASTEXITCODE -ne 0) {
  Copy-Item "$b\faceswap_api.py.bak_lookpack" "$b\faceswap_api.py" -Force
  Write-Output 'COMPILE_FAIL_ROLLED_BACK'
} else {
  $owners = Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object -ExpandProperty OwningProcess -Unique
  foreach($p in $owners){ if($p){ Stop-Process -Id $p -Force } }
  Stop-ScheduledTask -TaskName 'FaceSwap_Boot'
  Start-Sleep 3
  Start-ScheduledTask -TaskName 'FaceSwap_Boot'
  Write-Output 'DEPLOYED'
}
'@
  $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($remote))
  $out = ssh -o ConnectTimeout=10 "$User@$RemoteHost" "powershell -NoProfile -EncodedCommand $b64" 2>&1
  if ("$out" -match 'COMPILE_FAIL') { Die '远端 py_compile 失败，已自动回滚旧版' }
  if ("$out" -notmatch 'DEPLOYED') { Die "远端部署输出异常：$out" }
  Ok '远端入位 + 计划任务重启已触发'
  if (-not (Wait-Health "http://${RemoteHost}:8000/health" 150)) {
    # 回滚：还原备份再重启一次
    $rb = @'
$ErrorActionPreference='SilentlyContinue'
$b='C:\模仿音色'
Copy-Item "$b\faceswap_api.py.bak_lookpack" "$b\faceswap_api.py" -Force
$owners = Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object -ExpandProperty OwningProcess -Unique
foreach($p in $owners){ if($p){ Stop-Process -Id $p -Force } }
Stop-ScheduledTask -TaskName 'FaceSwap_Boot'; Start-Sleep 3; Start-ScheduledTask -TaskName 'FaceSwap_Boot'
Write-Output 'ROLLED_BACK'
'@
    $b64r = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($rb))
    ssh "$User@$RemoteHost" "powershell -NoProfile -EncodedCommand $b64r" 2>&1 | Out-Null
    Die '.104 换脸 150s 未就绪 → 已回滚旧版并重启。请看远端日志再试。'
  }
  Ok '.104 换脸健康'
  try {
    # openapi 生成在本项目 fastapi/pydantic 组合上本地远端都 500（与部署无关）
    # → 改功能级验证：打一发带 makeup 的换脸请求，响应含 makeup_ms 即新码在跑
    $probe = & $Py (Join-Path $Base 'tools\_verify_104_makeup2.py') 2>&1 | Out-String
    if ($probe -match 'makeup_ms in response: True') { Ok '直播妆容层功能级验证通过（makeup_ms 实测返回）' }
    else { Write-Host '  [WARN] 妆容层功能验证未通过（看 tools\_verify_104_makeup2.py 输出人工复核）' -ForegroundColor Yellow }
  } catch { Write-Host '  [WARN] 妆容层功能验证跳过' -ForegroundColor Yellow }
} else { Step '按 -SkipRemote 跳过 .104 部署' }

# ---- 4) 冒烟：临时角色定妆链 ----------------------------------------------
Step '冒烟测试（临时角色，不动现有角色）'
try {
  $mk = Invoke-RestMethod 'http://127.0.0.1:8004/health' -TimeoutSec 4
} catch { $mk = $null }
if (-not $mk -or -not $mk.model_loaded) {
  Write-Host '  [SKIP] 妆容服务(8004)不在线，跳过冒烟（start_makeup_api.bat 可拉起）' -ForegroundColor Yellow
} else {
  function PostJson([string]$url, [string]$json) {
    return Invoke-RestMethod -Method Post -Uri $url -ContentType 'application/json; charset=utf-8' `
                             -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 90
  }
  $act = (Invoke-RestMethod 'http://127.0.0.1:9000/health' -TimeoutSec 6).active_profile
  if (-not $act) { $act = 'Inside' }
  $face = (Invoke-RestMethod ("http://127.0.0.1:9000/profiles/{0}?include_face=true" -f [uri]::EscapeDataString($act)) -TimeoutSec 15).face_b64
  if (-not $face) { Write-Host "  [SKIP] 激活角色 $act 无人脸，跳过冒烟" -ForegroundColor Yellow }
  else {
    try { Invoke-RestMethod -Method Delete 'http://127.0.0.1:9000/profiles/_lookpack_smoke' -TimeoutSec 10 | Out-Null } catch {}
    PostJson 'http://127.0.0.1:9000/profiles' (@{name='_lookpack_smoke'; face_b64=$face} | ConvertTo-Json -Compress) | Out-Null
    $r = PostJson 'http://127.0.0.1:9000/api/profiles/_lookpack_smoke/makeup_preset' '{"style":"自然裸妆","apply":false}'
    if ($r.ok -and $r.preview_image) { Ok "makeup_preset 冒烟通过（$($r.elapsed_ms)ms, applied=$($r.applied.PSObject.Properties.Name -join ','))" }
    else { Write-Host '  [WARN] 冒烟返回异常' -ForegroundColor Yellow }
    $det = Invoke-RestMethod 'http://127.0.0.1:9000/profiles/_lookpack_smoke' -TimeoutSec 10
    if ($det.live_makeup -and $det.live_makeup.lip_color) { Ok '定妆→live_makeup 建议色联动生效' }
    Invoke-RestMethod -Method Delete 'http://127.0.0.1:9000/profiles/_lookpack_smoke' -TimeoutSec 10 | Out-Null
    Ok '临时角色已清理'
  }
}

Step '完成'
Write-Host @"
  激活完毕。使用入口：
   · 开播页 C-3 定妆块：妆容样式 + 💄妆容定妆 / ✨一键定妆包 / 💋直播妆容层
   · 试妆台 http://127.0.0.1:8004/ui
  本日志: $LogF
"@
Stop-Transcript | Out-Null
