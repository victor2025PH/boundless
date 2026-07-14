<#
  harden_remote.ps1 — 跨机服务面加固一键工具（从 hub/5090 远程执行）
  =====================================================================
  对每台"服务机"自动完成：
    1) 分发 service_auth.py + _auth_patch.py（scp 到 ASCII 暂存，再入项目目录）
    2) 写 secrets\service_token.txt + service_allow_ips.txt(=hub IP)
    3) 给每个 server 文件注入 service_auth.secure()（幂等、先备份 .bak_auth、py_compile 闸门）
    4) 固化 env：机器级 AVATARHUB_SERVICE_TOKEN + ALLOW_IPS（抗"文件被删"）
    5) 重启对应计划任务（先杀端口进程——boot 脚本"端口已监听则跳过"，光重启任务不生效），等健康
    6) 验证：hub 放行(非401) / 他机阻断(401) / 带令牌通(非401) / /health 放行(200)

  用法（在 C:\模仿音色 下，PowerShell）：
    .\harden_remote.ps1                  # 对拓扑内全部机器 deploy + verify
    .\harden_remote.ps1 -Mode verify     # 只复验，不改动（附带拓扑一致性 lint）
    .\harden_remote.ps1 -Mode rotate     # 轮换令牌：生成新令牌→三机 env+文件齐换→滚动重启→验证(旧令牌应失效)
    .\harden_remote.ps1 -Mode rollback   # 还原 .bak_auth + 删 env（应急回滚）
    .\harden_remote.ps1 -Mode drill      # 火警演习（无参=按周轮转目标；直播中自动让路）
    .\harden_remote.ps1 -HostCsv 192.168.0.104   # 只处理指定机
  幂等：可重复运行；已注入的文件会跳过，env/文件 重复写无害。
  轮换说明：service_token() 取值 env 优先于文件，运行中进程的旧 env 已冻结，故轮换必须重启服务
            才生效；重启窗口内 hub→服务仍可达(hub 在 IP 白名单内，与令牌无关)，主链不中断。

  拓扑来源：cluster_map.json（单一数据源，2026-07-05 起不再内嵌 MAP——
    上次搬迁就是因为脚本内嵌拓扑与 env_config.bat 各存一份、改了一处漏另一处，
    才有误报 critical + 自愈 1199 连败。改机器只改 cluster_map.json 一个文件）。
  旧机 192.168.1.43 / 192.168.1.51 已退役（演习/巡检打离线机=误报 critical，2026-07-05 事故根因）。
#>
[CmdletBinding()]
param(
  [ValidateSet('deploy','verify','rollback','rotate','drill')] [string]$Mode = 'deploy',
  [string[]]$Hosts,
  [string]$HostCsv,                                   # 逗号分隔的主机列表(供 -File 调用时稳妥传参，自身 split)
  [string]$ServiceCsv,                                # 逗号分隔的服务名(精准自愈：只动故障服务，不扰同机健康服务)
  [string]$User = 'Administrator',
  [string]$HubIp
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

# ---- 服务拓扑：从 cluster_map.json 加载（缺失/损坏=硬失败，宁停不带错跑）----
$MapFile = Join-Path $PSScriptRoot 'cluster_map.json'
if (-not (Test-Path $MapFile)) { throw "cluster_map.json 缺失（拓扑单一数据源）：$MapFile" }
$CM = Get-Content $MapFile -Raw -Encoding UTF8 | ConvertFrom-Json
$MAP = @{}
foreach ($prop in $CM.hosts.PSObject.Properties) {
  $hc = $prop.Value
  $svcs = @()
  foreach ($s in $hc.svcs) {
    $svcs += @{ server = [string]$s.server; name = [string]$s.name; addcors = [bool]$s.addcors
                task = [string]$s.task; port = [int]$s.port; loadkey = [string]$s.loadkey }
  }
  $MAP[$prop.Name] = @{ Dir = [string]$hc.dir; Py = [string]$hc.py; Svcs = $svcs }
}
if ($MAP.Count -eq 0) { throw 'cluster_map.json 的 hosts 为空' }
$HubPort = if ($CM.hub -and $CM.hub.port) { [int]$CM.hub.port } else { 9000 }

if ($HostCsv) { $Hosts = ($HostCsv -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
if (-not $Hosts) { $Hosts = @($MAP.Keys) }

# ---- hub 局域网 IP（白名单值）----
if (-not $HubIp) {
  $HubIp = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -like '192.168.*' } |
    Select-Object -First 1 -ExpandProperty IPAddress)
}
if (-not $HubIp) { throw 'no LAN hub IP detected; pass -HubIp x.x.x.x' }

# ---- 本机令牌（rotate=生成新令牌并备份旧的；否则缺则生成、有则复用）----
if (-not (Test-Path 'secrets')) { New-Item -ItemType Directory secrets | Out-Null }
$py = "$env:USERPROFILE\Miniconda3\envs\facefusion\python.exe"
if (-not (Test-Path $py)) { $py = 'python' }
$OldTok = ''
if ($Mode -eq 'rotate') {
  if (Test-Path 'secrets\service_token.txt') {
    $OldTok = (Get-Content 'secrets\service_token.txt' -Raw).Trim()
    Copy-Item 'secrets\service_token.txt' 'secrets\service_token.txt.old' -Force
  }
  $new = (& $py -c "import secrets;print(secrets.token_urlsafe(32))").Trim()
  Set-Content -Path 'secrets\service_token.txt' -Value $new -NoNewline -Encoding ascii
  [Environment]::SetEnvironmentVariable('AVATARHUB_SERVICE_TOKEN', $new, 'User')   # hub(本机用户级)
  Write-Host "[rotate] 新令牌已生成；旧令牌备份至 secrets\service_token.txt.old" -ForegroundColor Yellow
} elseif (-not (Test-Path 'secrets\service_token.txt')) {
  & $py -c "import secrets,pathlib;pathlib.Path('secrets/service_token.txt').write_text(secrets.token_urlsafe(32),encoding='ascii')"
}
$TOK = (Get-Content 'secrets\service_token.txt' -Raw).Trim()
Write-Host "hub IP = $HubIp | token len = $($TOK.Length) | mode = $Mode | 拓扑=cluster_map.json($($MAP.Count)机)" -ForegroundColor Cyan

function Invoke-Remote([string]$h, [string]$psText) {
  $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($psText))
  ssh -o ConnectTimeout=10 "$User@$h" "powershell -NoProfile -EncodedCommand $b64" 2>&1
}

function Probe([string]$url, [hashtable]$headers) {
  try { return (Invoke-WebRequest $url -Headers $headers -UseBasicParsing -TimeoutSec 5).StatusCode }
  catch { $sc = $_.Exception.Response.StatusCode.value__; if ($sc) { return $sc } else { return -1 } }
}

# 选一台"非目标"机做阻断验证（其源 IP 不在目标白名单内）。取 MAP 全集而非 -Hosts 子集：
# 单机精准自愈时(-HostCsv 只有目标机)也仍有他机可用作探测。
function Other-Host([string]$target) { return ($MAP.Keys | Where-Object { $_ -ne $target } | Select-Object -First 1) }

# 重启远端服务：先杀端口占用进程，再重跑计划任务。
# boot 脚本自带"端口已监听则跳过"幂等闸门 → 只 Stop/Start-ScheduledTask 不会真正重启 python
#（任务跑的是 .bat，spawn python 后即退出；Stop-ScheduledTask 杀不到 python）。
function Restart-RemoteServices([string]$h, [array]$svcs) {
  $rs = @("`$ErrorActionPreference='SilentlyContinue'")
  foreach ($s in $svcs) {
    $rs += "`$owners = Get-NetTCPConnection -LocalPort $($s.port) -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique"
    $rs += "foreach(`$p in `$owners){ if(`$p){ Stop-Process -Id `$p -Force -ErrorAction SilentlyContinue } }"
    $rs += "Stop-ScheduledTask -TaskName '$($s.task)' -ErrorAction SilentlyContinue"
  }
  $rs += "Start-Sleep 3"
  foreach ($s in $svcs) { $rs += "Start-ScheduledTask -TaskName '$($s.task)'" }
  Invoke-Remote $h ($rs -join "`n") | Out-Null
}

# 远端探测脚本（在"他机"上跑：无令牌应 401、带令牌应非 401）
function Get-ProbeScript([string]$base, [string]$tok) {
  $t = @'
$ErrorActionPreference='SilentlyContinue'
function p($u,$hd){ try{ return (Invoke-WebRequest $u -Headers $hd -UseBasicParsing -TimeoutSec 5).StatusCode }catch{ $c=$_.Exception.Response.StatusCode.value__; if($c){return $c}else{return -1} } }
Write-Output ('NO=' + (p '__BASE__/__authprobe' @{}) + ' YES=' + (p '__BASE__/__authprobe' @{'X-AH-Svc'='__TOK__'}))
'@
  return $t.Replace('__BASE__', $base).Replace('__TOK__', $tok)
}

$WantSvc = if ($ServiceCsv) { @($ServiceCsv -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) } else { $null }

# ============ 火警演习 drill：受控去护 → 验证探测网会响 → 精准复原 → 复验 ============
# 全程可逆；写本地 flag 让看门狗本轮让路(防复原 deploy 竞态)；finally 兜底复原+删flag。
if ($Mode -eq 'drill') {
  # -- 直播避让闸：演习要重启目标服务(断流~1-2分钟)。开播(真人换脸)或同传会话进行中
  #    一律顺延到下周任务，绝不为了演习打断生产。跳过时不写 drill_result.json(告警状态不动)。
  $liveBusy = @(); $liveCodes = @()
  try {
    $rt = (Invoke-WebRequest "http://127.0.0.1:${HubPort}/realtime/status" -UseBasicParsing -TimeoutSec 4).Content | ConvertFrom-Json
    if ($rt.video_running) { $liveBusy += '真人换脸开播中'; $liveCodes += 'realtime_faceswap' }
  } catch {}
  try {
    $it = (Invoke-WebRequest 'http://127.0.0.1:7900/health' -UseBasicParsing -TimeoutSec 4).Content | ConvertFrom-Json
    if ($it.running) { $liveBusy += '同传会话进行中'; $liveCodes += 'interp_session' }
  } catch {}
  if ($liveBusy.Count -gt 0 -and $HostCsv) {
    # 显式指定目标=操作员手动介入：不拦截，只提醒(负责人已知情，如深夜维护窗手动演练)
    Write-Host "[drill] 注意：$($liveBusy -join '、')，手动指定目标仍将继续" -ForegroundColor Yellow
    $liveBusy = @()
  }
  if ($liveBusy.Count -gt 0) {
    Write-Host "[drill] 直播避让：$($liveBusy -join '、')，本次演习顺延（不动服务、不改告警）" -ForegroundColor Yellow
    # 顺延要可见：发一次性事件(不进活动告警)。否则僵死会话会让演习被静默永久顺延。
    try {
      $pyArg = "import sys;sys.path.insert(0,r'$PSScriptRoot');import alerts;alerts.notify_event('auth drill deferred (live busy)', detail='" + ($liveCodes -join ',') + "', level='warn', source='harden_remote/drill')"
      & $py -c $pyArg 2>$null | Out-Null
    } catch {}
    exit 0
  }

  # -- 目标选择：显式参数优先；否则按 ISO 周号轮转全部 (机,服务) —— 每周演不同目标，
  #    三目标三周一轮，避免"永远只演 .104/faceswap、其余目标的复原链路从未实弹验证"。
  if ($HostCsv) {
    $dh = $Hosts[0]
    $dsName = if ($WantSvc) { $WantSvc[0] } else { $MAP[$dh].Svcs[0].name }
  } else {
    $allPairs = @()
    foreach ($k in ($MAP.Keys | Sort-Object)) { foreach ($s in $MAP[$k].Svcs) { $allPairs += ,@($k, $s.name) } }
    # ISO 周号(Thursday 规则)——与 hub 侧 python isocalendar() 完全同式，保证 /ops "下次演习预告"与实际选择一致
    $d0 = (Get-Date).Date; $thu = $d0.AddDays(3 - ((([int]$d0.DayOfWeek) + 6) % 7))
    $week = [int][Math]::Floor(($thu.DayOfYear - 1) / 7) + 1
    $pick = $allPairs[$week % $allPairs.Count]
    $dh = $pick[0]; $dsName = $pick[1]
    Write-Host "[drill] 周轮转选目标：ISO周$week % $($allPairs.Count) → $dh/$dsName" -ForegroundColor Cyan
  }
  $hostCfg = $MAP[$dh]
  if (-not $hostCfg) { Write-Host "drill: 目标 $dh 不在拓扑中，放弃" -ForegroundColor Red; exit 2 }
  $svc = $hostCfg.Svcs | Where-Object { $_.name -eq $dsName } | Select-Object -First 1
  if (-not $svc) { Write-Host "drill: 目标 $dh/$dsName 不在拓扑中，放弃" -ForegroundColor Red; exit 2 }
  $drillDir = $hostCfg.Dir
  $other = Other-Host $dh
  if (-not $other) { Write-Host "drill: 无他机可做阻断探测，放弃" -ForegroundColor Red; exit 2 }
  $base = "http://${dh}:$($svc.port)"
  $flag = Join-Path $PSScriptRoot 'logs\drill_active.flag'
  $flagDir = Split-Path $flag
  if (-not (Test-Path $flagDir)) { New-Item -ItemType Directory $flagDir | Out-Null }

  Write-Host "`n===== 火警演习 drill: $dh/$dsName (port $($svc.port)) 探测机=$other =====" -ForegroundColor Magenta
  Set-Content -Path $flag -Value (Get-Date -Format o) -Encoding ascii
  $drillPass = $false; $restored = $false; $detail = ''
  try {
    # 1) 受控去护：还原 .bak_auth + 重启该服务
    $unprotect = @"
`$ErrorActionPreference='SilentlyContinue'
`$f='$drillDir\$($svc.server)'
if(-not (Test-Path "`$f.bak_auth")){ Write-Output 'NO_BAKAUTH'; exit 0 }
Copy-Item "`$f.bak_auth" `$f -Force
Write-Output 'UNPROTECT_OK'
"@
    $u = (Invoke-Remote $dh $unprotect) -join ' '
    if ($u -match 'NO_BAKAUTH') { $detail = "缺 .bak_auth，无法演练"; throw $detail }
    if ($u -notmatch 'UNPROTECT_OK') { $detail = "去护失败: $u"; throw $detail }
    Restart-RemoteServices $dh @($svc)
    Start-Sleep 8
    for ($i=0; $i -lt 20; $i++) { try { Invoke-WebRequest "$base/health" -UseBasicParsing -TimeoutSec 3 | Out-Null; break } catch { Start-Sleep 2 } }

    # 2) 验证"探测网会响"：他机无令牌探测应 != 401（即裸奔被发现）
    $pr = (Invoke-Remote $other (Get-ProbeScript $base $TOK)) -join ' '
    $no = if ($pr -match 'NO=(\-?\d+)') { $matches[1] } else { '?' }
    if ($no -eq '401') { $detail = "探测网未发现裸奔(他机NO=$no)，探测失效！"; throw $detail }
    Write-Host "  [1/2] 探测网正常：裸奔被发现 (他机无令牌 NO=$no, 期望非401)" -ForegroundColor Green

    # 3) 精准复原（只动该服务）+ 4) 复验
    & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Mode deploy -HostCsv $dh -ServiceCsv $dsName | Out-Null
    $restored = $true
    & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Mode verify -HostCsv $dh -ServiceCsv $dsName | Out-Null
    $vexit = $LASTEXITCODE
    if ($vexit -ne 0) { $detail = "复原后复验未过(exit=$vexit)"; throw $detail }
    Write-Host "  [2/2] 精准复原成功，复验全绿" -ForegroundColor Green
    $drillPass = $true
  } catch {
    if (-not $detail) { $detail = $_.Exception.Message }
    Write-Host "  drill 异常: $detail" -ForegroundColor Red
  } finally {
    if (-not $restored) {
      Write-Host "  [兜底] 强制复原 $dh/$dsName ..." -ForegroundColor Yellow
      & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Mode deploy -HostCsv $dh -ServiceCsv $dsName | Out-Null
    }
    Remove-Item $flag -Force -ErrorAction SilentlyContinue
  }
  $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  # 结果接入告警三出口（写 UTF-8 结果文件 → 调 python 辅助，避开 PS 传中文编码坑）
  try {
    $res = @{ result = $(if ($drillPass) { 'PASS' } else { 'FAIL' }); target = "$dh/$dsName"; detail = $detail; ts = $stamp } | ConvertTo-Json -Compress
    Set-Content -Path (Join-Path $PSScriptRoot 'logs\drill_result.json') -Value $res -Encoding UTF8
    & $py (Join-Path $PSScriptRoot '_drill_alert.py') 2>$null
  } catch {}
  if ($drillPass) {
    Write-Host "[$stamp] DRILL PASS: 探测→检出→精准复原→复验 全链路有效" -ForegroundColor Cyan
    exit 0
  } else {
    Write-Host "[$stamp] DRILL FAIL: $detail" -ForegroundColor Red
    exit 1
  }
}

$Failures = @()
$Offline  = @()
foreach ($h in $Hosts) {
  $hostCfg = $MAP[$h]
  if (-not $hostCfg) { Write-Host "[$h] 不在拓扑中，跳过" -ForegroundColor Yellow; continue }
  $projectDir = $hostCfg.Dir
  $remotePy   = $hostCfg.Py
  $svcs = $hostCfg.Svcs
  if ($WantSvc) {
    $svcs = @($svcs | Where-Object { $WantSvc -contains $_.name })
    if (-not $svcs) { continue }   # 该机无匹配的故障服务，跳过
  }
  Write-Host "`n========== $h ($Mode) dir=$projectDir ==========" -ForegroundColor Green

  if ($Mode -eq 'rollback') {
    $lines = @("`$ErrorActionPreference='SilentlyContinue'", "`$b='$projectDir'")
    foreach ($s in $svcs) {
      $lines += "if(Test-Path ""`$b\$($s.server).bak_auth""){ Copy-Item ""`$b\$($s.server).bak_auth"" ""`$b\$($s.server)"" -Force; Write-Output 'restored $($s.server)' }"
    }
    $lines += "[Environment]::SetEnvironmentVariable('AVATARHUB_SERVICE_TOKEN',`$null,'Machine')"
    $lines += "[Environment]::SetEnvironmentVariable('AVATARHUB_SERVICE_ALLOW_IPS',`$null,'Machine')"
    $lines += "Write-Output 'rollback done (env cleared, servers restored)'"
    Invoke-Remote $h ($lines -join "`n")
    Restart-RemoteServices $h $svcs
    continue
  }

  $apply = ($Mode -eq 'deploy' -or $Mode -eq 'rotate')

  if ($Mode -eq 'deploy') {
    # 1) 分发文件（scp 到远端 ASCII 根目录暂存，避开中文路径的 scp 编码坑）
    scp -o ConnectTimeout=10 service_auth.py _auth_patch.py "$User@${h}:/" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "[$h] scp 失败" -ForegroundColor Red; continue }

    # 2) 入项目目录 + 逐服务补丁(编译闸门)
    $sb = New-Object Text.StringBuilder
    [void]$sb.AppendLine("`$ErrorActionPreference='Stop'")
    [void]$sb.AppendLine("`$b='$projectDir'")
    [void]$sb.AppendLine("`$py='$remotePy'")
    [void]$sb.AppendLine("if(-not (Test-Path `$py)){ `$py='C:\Miniconda3\envs\facefusion\python.exe' }")
    [void]$sb.AppendLine("if(-not (Test-Path `$py)){ `$py='C:\Miniconda3\envs\cosytts\python.exe' }")
    [void]$sb.AppendLine("`$src=`$null; foreach(`$c in @('C:\service_auth.py',(Join-Path `$env:USERPROFILE 'service_auth.py'))){ if(Test-Path `$c){ `$src=Split-Path `$c; break } }")
    [void]$sb.AppendLine("if(-not `$src){ Write-Output 'STAGED_NOT_FOUND'; exit 1 }")
    [void]$sb.AppendLine("Copy-Item (Join-Path `$src 'service_auth.py') ""`$b\service_auth.py"" -Force")
    [void]$sb.AppendLine("Copy-Item (Join-Path `$src '_auth_patch.py') ""`$b\_auth_patch.py"" -Force")
    foreach ($s in $svcs) {
      $ac = if ($s.addcors) { 'True' } else { 'False' }
      [void]$sb.AppendLine("& `$py ""`$b\_auth_patch.py"" ""`$b\$($s.server)"" $($s.name) $ac")
      [void]$sb.AppendLine("& `$py -c ""import py_compile;py_compile.compile(r'$projectDir\$($s.server)',doraise=True);print('COMPILE_OK $($s.server)')""")
    }
    [void]$sb.AppendLine("Write-Output 'PATCH_OK'")
    $out = Invoke-Remote $h $sb.ToString()
    $out | ForEach-Object { Write-Host "  $_" }
    if (-not (($out | Out-String) -match 'PATCH_OK')) { Write-Host "[$h] 补丁失败，跳过" -ForegroundColor Red; continue }
  }

  if ($apply) {
    # 写令牌/白名单文件 + 固化 env（deploy 与 rotate 共用；顺带覆盖搬迁前残留的旧 hub IP/旧令牌 env）
    $ap = New-Object Text.StringBuilder
    [void]$ap.AppendLine("`$ErrorActionPreference='Stop'")
    [void]$ap.AppendLine("`$b='$projectDir'")
    [void]$ap.AppendLine("if(-not (Test-Path ""`$b\secrets"")){ New-Item -ItemType Directory ""`$b\secrets"" | Out-Null }")
    [void]$ap.AppendLine("Set-Content -Path ""`$b\secrets\service_token.txt"" -Value '$TOK' -NoNewline -Encoding ascii")
    [void]$ap.AppendLine("Set-Content -Path ""`$b\secrets\service_allow_ips.txt"" -Value '$HubIp' -NoNewline -Encoding ascii")
    [void]$ap.AppendLine("[Environment]::SetEnvironmentVariable('AVATARHUB_SERVICE_TOKEN','$TOK','Machine')")
    [void]$ap.AppendLine("[Environment]::SetEnvironmentVariable('AVATARHUB_SERVICE_ALLOW_IPS','$HubIp','Machine')")
    [void]$ap.AppendLine("Write-Output 'APPLY_OK'")
    $out2 = Invoke-Remote $h $ap.ToString()
    if (-not (($out2 | Out-String) -match 'APPLY_OK')) { Write-Host "[$h] 写令牌/env 失败: $out2" -ForegroundColor Red; continue }

    # 重启（杀端口进程 + 重跑任务），等健康
    Restart-RemoteServices $h $svcs
    foreach ($s in $svcs) {
      $ok = $false
      for ($i=0; $i -lt 45; $i++) {
        Start-Sleep 2
        try {
          $r = Invoke-WebRequest "http://${h}:$($s.port)/health" -UseBasicParsing -TimeoutSec 3
          if ($r.StatusCode -eq 200 -and ([string]::IsNullOrEmpty($s.loadkey) -or $r.Content -match $s.loadkey)) { $ok = $true; break }
        } catch {}
      }
      Write-Host ("  [{0}] {1}:{2} healthy={3}" -f $s.name, $h, $s.port, $ok) -ForegroundColor $(if($ok){'Green'}else{'Red'})
    }
  }

  # ---- 验证（所有模式共用；rotate 额外校验旧令牌已失效）----
  # 三态判定：OK / CHECK(裸奔) / OFFLINE(不可达)。
  #  · 鉴权巡检只回答"可达的服务是否被正确保护"。不可达是"可用性"问题、归
  #    supervisor 管，不是"鉴权回归"——若把离线机判成裸奔，会触发对离线机
  #    永远无效的 deploy 自愈，形成告警风暴/自愈死循环(线上事故根因)。
  #  · 跨机"无令牌应被拒"校验依赖"探测他机"在线；探测机本身离线时，拿不到
  #    NO= 结果就不反向扣分(否则探测机宕机会把健康目标误判成裸奔)。
  $other = Other-Host $h
  foreach ($s in $svcs) {
    $base = "http://${h}:$($s.port)"
    $hubProbe = Probe "$base/__authprobe" @{}                       # 从 hub(本机, 白名单)
    $health   = Probe "$base/health" @{}
    $line = "  [{0}] hub无令牌={1}(期望非401) /health={2}(期望200)" -f $s.name, $hubProbe, $health
    # 可达性：拿到任意 HTTP 响应(状态码>0)即在线；health 与鉴权探针都 <=0 即不可达
    $reachable = ($health -gt 0) -or ($hubProbe -gt 0)
    $oldOk = $true
    $crossOk = $true            # 跨机"无令牌应 401"；探测机不可用→无法判定→不扣分
    if ($other -and $reachable) {
      $r = (Invoke-Remote $other (Get-ProbeScript $base $TOK)) -join ' '
      $line += " | 他机($other)$r (期望 NO=401 YES=非401)"
      if ($r -match 'NO=(\-?\d+)') { $crossOk = ($matches[1] -eq '401') }
      else { $line += ' [探测机不可用,跨机校验跳过]' }
      if ($Mode -eq 'rotate' -and $OldTok) {
        $ro = (Invoke-Remote $other (Get-ProbeScript $base $OldTok)) -join ' '
        $oldYes = if ($ro -match 'YES=(\-?\d+)') { $matches[1] } else { '?' }
        $line += " | 旧令牌=$oldYes(期望401失效)"
        $oldOk = ($oldYes -eq '401')
      }
    }
    if (-not $reachable) {
      $verdict = 'OFFLINE'
    } elseif ($hubProbe -ne 401 -and $health -eq 200 -and $crossOk -and $oldOk) {
      $verdict = 'OK'
    } else {
      $verdict = 'CHECK'
    }
    if ($verdict -eq 'CHECK')   { $Failures += "$h/$($s.name)" }
    if ($verdict -eq 'OFFLINE') { $Offline  += "$h/$($s.name)" }
    $col = switch ($verdict) { 'OK' { 'Green' } 'OFFLINE' { 'DarkYellow' } default { 'Yellow' } }
    Write-Host "$line => $verdict" -ForegroundColor $col
  }
}

# ---- 拓扑一致性 lint（verify 全量时附带）：cluster_map.json vs env_config.bat 路由 ----
# 漂移≠裸奔：lint 自管 topology_drift 告警(raise/clear)，绝不影响本脚本退出码——
# 否则看门狗会对"配置漂移"发起注定无效的 deploy 自愈(1199 连败的教训)。
if ($Mode -eq 'verify' -and -not $HostCsv -and -not $ServiceCsv) {
  $lint = Join-Path $PSScriptRoot 'tools\topology_lint.py'
  if (Test-Path $lint) {
    try { & $py $lint 2>&1 | ForEach-Object { Write-Host "  [lint] $_" } } catch { Write-Host "  [lint] 执行异常(忽略): $($_.Exception.Message)" -ForegroundColor Yellow }
  }
}

$stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
$offNote = if ($Offline.Count -gt 0) { "（离线跳过 $($Offline.Count): $($Offline -join ', ')）" } else { '' }
if ($Failures.Count -gt 0) {
  # 仅"可达但裸奔"才算鉴权回归 → 退出码 1（看门狗据此精准自愈，只动可达的故障机）
  Write-Host "`n[$stamp][ALERT] 鉴权异常($($Failures.Count)): $($Failures -join ', ')$offNote" -ForegroundColor Red
  Write-Host "  可能原因：补丁被覆盖 / 令牌被清 / 服务未重启。修复：powershell -File harden_remote.ps1 -Mode deploy" -ForegroundColor Red
  exit 1
} elseif ($Offline.Count -gt 0) {
  # 全部"非 OK"都是不可达：可用性问题、非鉴权问题 → 退出码 0，不触发 deploy 自愈
  Write-Host "`n[$stamp] 鉴权正常（在线服务他机无令牌均被拒）。离线跳过($($Offline.Count)): $($Offline -join ', ')" -ForegroundColor DarkYellow
  exit 0
} else {
  Write-Host "`n[$stamp] 完成：全部服务鉴权正常（他机无令牌均被拒）。" -ForegroundColor Cyan
  exit 0
}
