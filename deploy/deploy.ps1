<#
  deploy.ps1 — wujie 全域起停/健康/provision 编排（Phase 7）
  单一源：deploy/stack.json（逻辑服务）+ deploy/cluster_map.json（跨机显存拓扑）
  设计：委派各引擎既有脚本（不重造），端口幂等，status 三态(GO/DEGRADED/DOWN)+退出码。

  用法：
    powershell -File deploy\deploy.ps1 -Action status               # 只读，默认剖面 core+web
    powershell -File deploy\deploy.ps1 -Action status -Profile all -Json
    powershell -File deploy\deploy.ps1 -Action up   -Profile core   # 幂等拉起(端口在听则跳过)
    powershell -File deploy\deploy.ps1 -Action up   -Only huoke -WhatIf
    powershell -File deploy\deploy.ps1 -Action down -Only website -Force
    powershell -File deploy\deploy.ps1 -Action provision            # 只读报缺口
    powershell -File deploy\deploy.ps1 -Action provision -Apply -From "D:\workspace\_workspace_backup_20260715"

  退出码(status)：0=GO 1=DEGRADED 2=DOWN（供 cron/监控消费）
#>
[CmdletBinding()]
param(
  [ValidateSet('status','up','down','provision')]
  [string]$Action = 'status',
  [string]$Profile = 'core,web',
  [string]$Only = '',
  [switch]$Json,
  [switch]$WhatIf,
  [switch]$Force,
  [switch]$Apply,
  [string]$From = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$DeployDir = $PSScriptRoot
$Root = Split-Path -Parent $DeployDir
$StackPath = Join-Path $DeployDir 'stack.json'
if (-not (Test-Path $StackPath)) { Write-Error "stack.json not found: $StackPath"; exit 3 }
$Stack = Get-Content -LiteralPath $StackPath -Raw -Encoding UTF8 | ConvertFrom-Json

function Resolve-Dir($svc) {
  $d = if ($svc.runtime_dir) { $svc.runtime_dir } else { $svc.dir }
  if ([System.IO.Path]::IsPathRooted($d)) { return $d }
  return (Join-Path $Root $d)
}

function Test-Port([int]$port) {
  try {
    $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    return [bool]$c
  } catch {
    $ns = netstat -ano | Select-String ":$port\s" | Select-String 'LISTENING'
    return [bool]$ns
  }
}

function Get-PortOwners([int]$port) {
  $pids = @()
  try {
    $pids = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
  } catch {
    $ns = netstat -ano | Select-String ":$port\s" | Select-String 'LISTENING'
    foreach ($l in $ns) { $p = ($l.Line -split '\s+' | Where-Object { $_ })[-1]; if ($p -match '^\d+$') { $pids += [int]$p } }
    $pids = $pids | Select-Object -Unique
  }
  return $pids
}

function Invoke-Health($url, $regex) {
  $res = @{ ok = $false; code = 0; match = $true; err = '' }
  if ([string]::IsNullOrWhiteSpace($url)) { return $res }
  try {
    $r = Invoke-WebRequest -Uri $url -TimeoutSec 4 -UseBasicParsing -ErrorAction Stop
    $res.code = [int]$r.StatusCode
    $res.ok = ($res.code -ge 200 -and $res.code -lt 300)
    if ($regex) { $res.match = [bool]([regex]::IsMatch([string]$r.Content, $regex)) }
  } catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code) { $res.code = [int]$code }
    $res.err = $_.Exception.Message
  }
  return $res
}

function Select-Services([switch]$IgnoreEnabled) {
  $prof = @($Profile -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  $only = @($Only -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  $out = @()
  foreach ($s in $Stack.services) {
    if ($only.Count) {
      if ($only -contains $s.id) { $out += $s }
      continue
    }
    $inProfile = ($prof -contains 'all')
    if (-not $inProfile) { foreach ($p in $prof) { if ($s.profiles -contains $p) { $inProfile = $true; break } } }
    if (-not $inProfile) { continue }
    # status(只读)看全部；up/down/provision 默认只作用于 enabled=true 的服务
    if ($IgnoreEnabled -or $s.enabled) { $out += $s }
  }
  return $out
}

function Get-ServiceState($svc) {
  $listening = $false; $openPort = 0
  foreach ($p in $svc.ports) { if (Test-Port ([int]$p)) { $listening = $true; $openPort = [int]$p; break } }
  $state = [ordered]@{ id = $svc.id; title = $svc.title; listening = $listening; port = $openPort; verdict = 'DOWN'; health = ''; note = '' }
  if (-not $listening) { $state.verdict = 'DOWN'; return [pscustomobject]$state }
  # listening
  if ($svc.health.url -and -not $svc.health.auth) {
    $h = Invoke-Health $svc.health.url $svc.health.regex
    if ($h.ok -and $h.match) { $state.verdict = 'GO'; $state.health = "200" }
    elseif ($h.ok -and -not $h.match) { $state.verdict = 'DEGRADED'; $state.health = "200 但未就绪(regex 未命中)" }
    elseif ($h.code) { $state.verdict = 'DEGRADED'; $state.health = "HTTP $($h.code)" }
    else { $state.verdict = 'DEGRADED'; $state.health = "端口在听但 /health 无响应" }
  } else {
    $state.verdict = 'GO'
    $state.health = if ($svc.health.auth) { '端口在听(health 需鉴权,按端口判活)' } else { '端口在听' }
  }
  return [pscustomobject]$state
}

function Do-Status {
  $svcs = Select-Services -IgnoreEnabled   # 只读：连 enabled=false 的探活服务(TTS 等)一并显示
  $states = @($svcs | ForEach-Object { Get-ServiceState $_ })
  $worst = 0
  foreach ($s in $states) { $lvl = switch ($s.verdict) { 'GO' {0} 'DEGRADED' {1} 'DOWN' {2} default {2} }; if ($lvl -gt $worst) { $worst = $lvl } }
  if ($Json) {
    $obj = [ordered]@{ timestamp = (Get-Date).ToString('o'); profile = $Profile; verdict = $worst
      verdict_label = @('GO','DEGRADED','DOWN')[$worst]; services = $states }
    $obj | ConvertTo-Json -Depth 6
    return $worst
  }
  Write-Host "==================== wujie deploy status ($Profile) ===================="
  foreach ($s in $states) {
    $color = switch ($s.verdict) { 'GO' {'Green'} 'DEGRADED' {'Yellow'} 'DOWN' {'Red'} default {'Gray'} }
    $portTxt = if ($s.port) { ":$($s.port)" } else { '(no port)' }
    Write-Host ("  [{0,-8}] {1,-12} {2,-10} {3}" -f $s.verdict, $s.id, $portTxt, $s.health) -ForegroundColor $color
  }
  Write-Host "------------------------------------------------------------------------"
  $label = @('GO','DEGRADED','DOWN')[$worst]
  $vc = @('Green','Yellow','Red')[$worst]
  Write-Host ("  VERDICT: [{0}] {1}" -f $worst, $label) -ForegroundColor $vc
  return $worst
}

function Launch-Service($svc) {
  $dir = Resolve-Dir $svc
  if (-not (Test-Path $dir)) { Write-Host ("  [SKIP] {0}: dir 不存在 {1}" -f $svc.id, $dir) -ForegroundColor Yellow; return }
  $via = $svc.up.via
  $desc = ''
  $exe = ''; $args = ''
  if ($via -eq 'script') {
    $script = $svc.up.script
    $sp = Join-Path $dir $script
    if (-not (Test-Path $sp)) { Write-Host ("  [SKIP] {0}: 启动脚本缺失 {1}" -f $svc.id, $sp) -ForegroundColor Yellow; return }
    $extra = if ($svc.up.args) { ' ' + $svc.up.args } else { '' }
    if ($script -match '\.ps1$') { $exe = 'powershell'; $args = "-NoProfile -ExecutionPolicy Bypass -File `"$sp`"$extra" }
    else { $exe = 'cmd.exe'; $args = "/c `"$sp`"$extra" }
    $desc = "$exe $args  (cwd=$dir)"
  } elseif ($via -eq 'inline') {
    $exe = 'cmd.exe'; $args = "/c $($svc.up.inline)"
    $desc = "$($svc.up.inline)  (cwd=$dir)"
  } else {
    Write-Host ("  [SKIP] {0}: up.via 未知 ({1})" -f $svc.id, $via) -ForegroundColor Yellow; return
  }
  if ($WhatIf) { Write-Host ("  [WHATIF] {0}: 将执行 -> {1}" -f $svc.id, $desc) -ForegroundColor Cyan; return }
  # set env
  if ($svc.env) { foreach ($k in $svc.env.PSObject.Properties.Name) { [Environment]::SetEnvironmentVariable($k, [string]$svc.env.$k, 'Process') } }
  Write-Host ("  [UP] {0}: {1}" -f $svc.id, $desc) -ForegroundColor Green
  Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $dir -WindowStyle Minimized | Out-Null
}

function Do-Up {
  $svcs = Select-Services
  foreach ($svc in $svcs) {
    $listening = $false
    foreach ($p in $svc.ports) { if (Test-Port ([int]$p)) { $listening = $true; break } }
    if ($listening) { Write-Host ("  [SKIP] {0}: 端口已在听(幂等跳过)" -f $svc.id) -ForegroundColor DarkGray; continue }
    if ($svc.gpu -and -not $Force -and -not ($Only)) {
      Write-Host ("  [SKIP] {0}: GPU/多机服务，默认不自动起。需显式 -Only {0} 或 -Force" -f $svc.id) -ForegroundColor Yellow
      continue
    }
    Launch-Service $svc
  }
  if (-not $WhatIf) { Write-Host "  提示：起后用 -Action status 复核就绪。" -ForegroundColor DarkGray }
}

function Do-Down {
  $svcs = Select-Services
  foreach ($svc in $svcs) {
    if ($svc.down.via -eq 'script') {
      $dir = Resolve-Dir $svc; $sp = Join-Path $dir $svc.down.script
      if (Test-Path $sp) {
        if ($WhatIf) { Write-Host ("  [WHATIF] {0}: 将运行停止脚本 {1}" -f $svc.id, $sp) -ForegroundColor Cyan; continue }
        Write-Host ("  [DOWN] {0}: {1}" -f $svc.id, $sp) -ForegroundColor Magenta
        if ($sp -match '\.ps1$') { & powershell -NoProfile -ExecutionPolicy Bypass -File $sp | Out-Null }
        else { & cmd.exe /c "`"$sp`"" | Out-Null }
        continue
      }
    }
    # by port
    $owners = @()
    foreach ($p in $svc.ports) { $owners += Get-PortOwners ([int]$p) }
    $owners = $owners | Select-Object -Unique
    if (-not $owners) { Write-Host ("  [SKIP] {0}: 未在跑" -f $svc.id) -ForegroundColor DarkGray; continue }
    if (-not $Force -and -not $WhatIf) { Write-Host ("  [HOLD] {0}: 将停 PID $($owners -join ',')；加 -Force 执行(或 -WhatIf 预览)" -f $svc.id) -ForegroundColor Yellow; continue }
    if ($WhatIf) { Write-Host ("  [WHATIF] {0}: 将停 PID $($owners -join ',')" -f $svc.id) -ForegroundColor Cyan; continue }
    foreach ($pid2 in $owners) { taskkill /PID $pid2 /T /F 2>&1 | Out-Null }
    Write-Host ("  [DOWN] {0}: 已停 PID $($owners -join ',')" -f $svc.id) -ForegroundColor Magenta
  }
}

function Test-RuntimeExe($svc) {
  switch ($svc.runtime) {
    'python' { return [bool](Get-Command python -ErrorAction SilentlyContinue) }
    'uv'     { return [bool](Get-Command uv -ErrorAction SilentlyContinue) }
    'npm'    { return [bool](Get-Command npm -ErrorAction SilentlyContinue) }
    'conda'  { return (Test-Path $svc.conda_python) }
    'batch'  { return $true }
    default  { return $true }
  }
}

function Do-Provision {
  $svcs = Select-Services
  Write-Host "==================== wujie provision 缺口体检 ($Profile) ===================="
  Write-Host "(只读；-Apply 才会动手，且仅做安全填补)"
  foreach ($svc in $svcs) {
    $dir = Resolve-Dir $svc
    Write-Host ""
    Write-Host ("● {0} [{1}]  dir={2}" -f $svc.id, $svc.title, $dir) -ForegroundColor Cyan
    $rt = Test-RuntimeExe $svc
    $rtMsg = if ($rt) { 'OK' } else { '缺失' }
    Write-Host ("   runtime({0}): {1}" -f $svc.runtime, $rtMsg) -ForegroundColor $(if ($rt) {'Green'} else {'Red'})
    # heuristic presence checks
    if ($svc.id -eq 'website') {
      $nm = Test-Path (Join-Path $dir 'node_modules')
      Write-Host ("   node_modules: {0}" -f $(if ($nm) {'OK'} else {'缺失(npm install)'})) -ForegroundColor $(if ($nm) {'Green'} else {'Yellow'})
    }
    if ($svc.id -eq 'chengjie') {
      # 只认真正的运行时机密文件(git 忽略)，config.example.yaml 不算
      $real = Test-Path (Join-Path $dir 'config\config.yaml')
      $sess = (@(Get-ChildItem (Join-Path $dir 'sessions') -Filter *.session -Recurse -ErrorAction SilentlyContinue).Count -gt 0)
      Write-Host ("   config/config.yaml(机密): {0}" -f $(if ($real) {'存在'} else {'缺失(从备份取或 python main.py --init)'})) -ForegroundColor $(if ($real) {'Green'} else {'Yellow'})
      Write-Host ("   sessions/(登录态):        {0}" -f $(if ($sess) {'存在'} else {'缺失(从备份取，否则需重新登录)'})) -ForegroundColor $(if ($sess) {'Green'} else {'Yellow'})
    }
    if ($svc.id -eq 'huoke') {
      $launchEnv = Test-Path (Join-Path $dir 'config\launch.env')
      $devices = Test-Path (Join-Path $dir 'config\devices.yaml')
      Write-Host ("   config/launch.env(可选):  {0}" -f $(if ($launchEnv) {'存在'} else {'缺省(port=18080)'})) -ForegroundColor $(if ($launchEnv) {'Green'} else {'DarkGray'})
      Write-Host ("   config/devices.yaml(运行态): {0}" -f $(if ($devices) {'存在'} else {'缺失(从备份取)'})) -ForegroundColor $(if ($devices) {'Green'} else {'Yellow'})
    }
    if ($svc.id -eq 'indextts') {
      $ck = Test-Path (Join-Path $dir 'checkpoints')
      Write-Host ("   checkpoints/: {0}" -f $(if ($ck) {'存在'} else {'缺失(uv sync + 权重下载)'})) -ForegroundColor $(if ($ck) {'Green'} else {'Yellow'})
    }
    foreach ($need in $svc.provision.needs) { Write-Host ("     - 需: {0}" -f $need) -ForegroundColor DarkGray }

    if ($Apply -and $From) {
      $srcDirName = switch ($svc.id) { 'chengjie' {'telegram-mtproto-ai'} 'huoke' {'mobile-auto0423'} default {''} }
      if ($srcDirName) {
        $srcCfg = Join-Path $From (Join-Path $srcDirName 'config')
        $dstCfg = Join-Path $dir 'config'
        if ((Test-Path $srcCfg) -and (Test-Path $dstCfg)) {
          $copied = 0
          Get-ChildItem $srcCfg -Filter *.yaml -File -ErrorAction SilentlyContinue | ForEach-Object {
            $target = Join-Path $dstCfg $_.Name
            if (-not (Test-Path $target)) { Copy-Item $_.FullName $target -Force; $copied++ }
          }
          Write-Host ("   [APPLY] 从备份补 config/*.yaml: 新增 $copied 个 (已存在的不覆盖)") -ForegroundColor Green
        } else {
          Write-Host ("   [APPLY] 跳过：备份源 config 不存在 ($srcCfg)") -ForegroundColor DarkYellow
        }
      }
    }
  }
  Write-Host ""
  Write-Host "provision 完成（体检）。要真正跑引擎：先补齐上面缺口，再 -Action up。" -ForegroundColor DarkGray
}

switch ($Action) {
  'status'    { $code = Do-Status; exit $code }
  'up'        { Do-Up }
  'down'      { Do-Down }
  'provision' { Do-Provision }
}
