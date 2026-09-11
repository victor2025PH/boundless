<#
  华灵网站 · 本地一键部署 (Windows / PowerShell)
  流程: 主树门禁 → sync:brand → 打包 website/ → 只上传部署包 → 服务器针定 deploy.sh 原子部署 → 公网体检
  绝不在脚本中存放密码：从 $env:VPS_PASS 读取，缺失则安全提示输入(SecureString)。
  2026-08-20：不再上传覆盖远端 deploy.sh（该文件 chattr +i 针定）；只允许主工作树部署。

  认证: 优先 SSH 密钥；找不到密钥文件时回退密码。
  传输: 优先本机 OpenSSH (scp/ssh)，无需 Posh-SSH；仅当 ssh 不可用时回退 Posh-SSH。

  用法:
    cd website
    pwsh ./scripts/deploy.ps1                     # 推荐（PowerShell 7+）
    ./scripts/deploy.ps1                          # Windows PS5 会自动改用 pwsh（若已安装）
    $env:VPS_PASS = '...'; ./scripts/deploy.ps1
    ./scripts/deploy.ps1 -FromHead            # 只发已提交 website/，不带工作树半成品
    ./scripts/deploy.ps1 -AllowDirty          # 明确允许脏树（默认拒绝）
    ./scripts/deploy.ps1 -FromHead -SkipAssets
#>
param(
  [string]$VpsHost      = $(if ($env:VPS_HOST) { $env:VPS_HOST } else { '165.154.233.121' }),
  [string]$User         = $(if ($env:VPS_USER) { $env:VPS_USER } else { 'ubuntu' }),
  [string]$RemoteDir    = '/home/ubuntu',
  [string]$SiteUrl      = $(if ($env:SITE_URL) { $env:SITE_URL } else { 'https://bd2026.cc' }),
  [string]$KeyFile      = $(if ($env:VPS_KEY) { $env:VPS_KEY } else { Join-Path $HOME '.ssh/hualing_deploy' }),
  [switch]$SkipAssets,  # 只发源码，跳过 public/downloads、public/releases 发布物同步
  [switch]$FromHead,    # 只打 git HEAD 里的 website/，不带工作树未提交改动
  [switch]$AllowDirty   # 明确允许把脏树打上生产（默认拒绝）
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5 对部分模块/SSH 体验差：若有 pwsh 则自动转发（避免「Import-Module Posh-SSH 失败」）
if ($PSVersionTable.PSEdition -ne 'Core') {
  $pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
  if ($pwshCmd) {
    Write-Host "检测到 Windows PowerShell $($PSVersionTable.PSVersion)；改用 pwsh 重新启动本脚本..."
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
      '-VpsHost', $VpsHost, '-User', $User, '-RemoteDir', $RemoteDir, '-SiteUrl', $SiteUrl, '-KeyFile', $KeyFile)
    if ($SkipAssets) { $argList += '-SkipAssets' }
    if ($FromHead) { $argList += '-FromHead' }
    if ($AllowDirty) { $argList += '-AllowDirty' }
    & $pwshCmd.Source @argList
    exit $LASTEXITCODE
  }
}

$WebRoot = Split-Path -Parent $PSScriptRoot

# 2026-08-20：只许从主工作树部署。并行 worktree 里的 website 停在 7/31–8/1，
# 从那里跑本脚本会把生产 rsync --delete 回旧版（8/19 一天两次）。
$resolvedRoot = (Resolve-Path $WebRoot).Path
$canonicalOk = $false
foreach ($c in @('D:\workspace\boundless\website', 'D:\boundless\website')) {
  if (Test-Path $c) {
    if ($resolvedRoot -eq (Resolve-Path $c).Path) { $canonicalOk = $true; break }
  }
}
if (-not $canonicalOk) {
  throw "拒绝部署：当前 website 根不在主工作树（$resolvedRoot）。请到 D:\boundless\website 再跑 scripts\deploy.ps1。"
}
if (-not (Test-Path (Join-Path $WebRoot 'app\pricing\page.tsx'))) {
  throw '拒绝部署：缺少 app/pricing/page.tsx，这是一份旧 website 树。'
}
if (-not (Test-Path (Join-Path $WebRoot '.deploy-epoch'))) {
  throw '拒绝部署：缺少 .deploy-epoch（旧树没有此文件）。'
}

# tar 名带 PID+时间戳：多条线（多 agent/多窗口）并发部署时各用各的临时包，
# 不再互踩「文件被另一进程占用」（2026-07-25 实际发生）。服务器端并发由 deploy.sh 的 flock 锁串行化。
$tar = Join-Path $env:TEMP ("website-deploy-{0}-{1}.tar.gz" -f $PID, (Get-Date -Format 'HHmmss'))
$sshCmd = Get-Command ssh -ErrorAction SilentlyContinue
$scpCmd = Get-Command scp -ErrorAction SilentlyContinue
$useOpenSsh = ($null -ne $sshCmd) -and ($null -ne $scpCmd)
$sshExe = if ($sshCmd) { $sshCmd.Source } else { $null }
$scpExe = if ($scpCmd) { $scpCmd.Source } else { $null }
$script:sshSession = $null
$script:keyPath = $null

function Invoke-Remote([string]$Command, [int]$TimeoutSec = 60) {
  if ($script:useOpenSsh) {
    $sshArgs = @('-o', 'StrictHostKeyChecking=accept-new', '-o', "ConnectTimeout=20")
    if ($script:keyPath) { $sshArgs += @('-i', $script:keyPath) }
    $sshArgs += @("$User@$VpsHost", $Command)
    $out = & $sshExe @sshArgs 2>&1
    return ($out | Out-String)
  }
  $r = Invoke-SSHCommand -SessionId $script:sshSession.SessionId -Command $Command -TimeOut $TimeoutSec
  return ($r.Output -join "`n")
}

# 从发布指针文件里取版本号：manifest.json 走 JSON 键，latest.yml（electron-updater）走首行。
function Get-PublishVersion([string]$Text) {
  if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
  $m = [regex]::Match($Text, '"version"\s*:\s*"([0-9][0-9.]*)"')
  if (-not $m.Success) { $m = [regex]::Match($Text, '(?m)^version:\s*([0-9][0-9.]*)\s*$') }
  if ($m.Success) { return $m.Groups[1].Value }
  return $null
}

$headStage = $null
Push-Location $WebRoot
try {
  Write-Host '[0/4] sync:brand（vendored 品牌 preset，缺则服务器 next build 必挂）...'
  & node scripts/sync-brand.mjs
  if ($LASTEXITCODE -ne 0) { throw 'sync:brand 失败' }
  & node scripts/sync-brand.mjs --check
  if ($LASTEXITCODE -ne 0) { throw 'sync:brand --check 失败：vendor/brand 与 platform/brand 不一致' }
  Write-Host '    [OK] vendor/brand 已与上游对齐'

  # 打包前写 .deploy-meta.json。commit_ts 取 website/ 目录最近一次提交（不是仓 HEAD），
  # 避免功能分支「仓更新、website 仍旧」骗过时间戳门禁。写失败直接中止。
  Write-Host '[0.5/4] 写部署元数据 (.deploy-meta.json: website 提交时间 + 部署机) ...'
  $gitHead = (git -C $WebRoot rev-parse HEAD 2>$null).Trim()
  $gitTsRaw = (git -C $WebRoot log -1 --format=%ct -- . 2>$null).Trim()
  if (-not $gitHead -or -not $gitTsRaw) { throw '读 git 元数据失败，拒绝打包' }
  $gitTs = [int]$gitTsRaw
  $gitDirty = -not [string]::IsNullOrWhiteSpace((git -C $WebRoot status --porcelain -- . 2>$null | Out-String).Trim())
  if ($gitDirty -and -not $FromHead -and -not $AllowDirty) {
    $dirtyN = @(git -C $WebRoot status --porcelain -- .).Count
    throw ("拒绝部署：website/ 工作树有 {0} 个未提交改动，打上去会把半成品带上生产。请用 -FromHead 只发已提交内容，或 -AllowDirty 明确带上工作树。" -f $dirtyN)
  }
  $packedFromHead = [bool]$FromHead
  @{ commit = $gitHead; commit_ts = $gitTs; dirty = ($(if ($packedFromHead) { $false } else { $gitDirty }))
     from_head = $packedFromHead
     packed_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
     host = $env:COMPUTERNAME; user = $env:USERNAME } |
    ConvertTo-Json -Compress | Set-Content -Path (Join-Path $WebRoot '.deploy-meta.json') -Encoding ascii
  Write-Host ("    website HEAD {0} @ {1}{2}" -f $gitHead.Substring(0,8), $gitTs, $(if ($packedFromHead) { ' (from HEAD, dirty tree ignored)' } elseif ($gitDirty) { ' (dirty tree)' } else { '' }))

  Write-Host '[1/4] 打包 website/ (排除 node_modules/.next/.git/.env.local/临时文件) ...'
  if (Test-Path $tar) { Remove-Item $tar -Force }
  # public/downloads、public/releases（安装包等大文件）不进源码包：服务器侧 rsync 已 exclude
  # 令其常驻，由下方 [3.5/4] 差量上传，避免每次部署重传数百 MB。
  $packRoot = $WebRoot
  if ($FromHead) {
    $repoRoot = Split-Path -Parent $WebRoot
    $headStage = Join-Path $env:TEMP ("website-head-{0}" -f $PID)
    if (Test-Path $headStage) { Remove-Item $headStage -Recurse -Force }
    New-Item -ItemType Directory -Path $headStage | Out-Null
    Write-Host '    -FromHead: git archive HEAD -- website （不带工作树半成品）'
    $archiveTar = Join-Path $env:TEMP ("website-head-src-{0}.tar" -f $PID)
    git -C $repoRoot archive --format=tar --output=$archiveTar HEAD -- website
    if ($LASTEXITCODE -ne 0) { throw 'git archive HEAD -- website 失败' }
    tar -xf $archiveTar -C $headStage
    Remove-Item $archiveTar -Force -ErrorAction SilentlyContinue
    $packRoot = Join-Path $headStage 'website'
    if (-not (Test-Path (Join-Path $packRoot 'app\pricing\page.tsx'))) {
      throw "FromHead 解包后缺少 app/pricing/page.tsx（$packRoot）"
    }
    Copy-Item (Join-Path $WebRoot '.deploy-meta.json') (Join-Path $packRoot '.deploy-meta.json') -Force
    $vendorSrc = Join-Path $WebRoot 'vendor\brand'
    $vendorDst = Join-Path $packRoot 'vendor\brand'
    if (Test-Path $vendorSrc) {
      New-Item -ItemType Directory -Path $vendorDst -Force | Out-Null
      Copy-Item (Join-Path $vendorSrc '*') $vendorDst -Force
    }
  }
  Push-Location $packRoot
  try {
    tar -czf $tar --exclude=node_modules "--exclude=.next*" --exclude=.git --exclude=.env.local `
        --exclude=.hand-shots --exclude=.robot-shots `
        "--exclude=*.tsbuildinfo" "--exclude=*.log" --exclude=og-test.png --exclude=test-fill.png `
        --exclude=ops-overlay.tgz "--exclude=scripts/_*" `
        --exclude=public/downloads --exclude=public/releases .
    if ($LASTEXITCODE -ne 0) { throw '打包失败' }
  } finally {
    Pop-Location
  }
  Write-Host ("    包大小 {0:N1} MB" -f ((Get-Item $tar).Length / 1MB))

  $script:keyPath = $null
  $passAuth = $false
  if ($KeyFile -and (Test-Path $KeyFile)) {
    $script:keyPath = (Resolve-Path $KeyFile).Path
    Write-Host "    认证方式: SSH 密钥 ($KeyFile)"
  } else {
    $passAuth = $true
    Write-Host "    认证方式: 密码（OpenSSH 路径下请改用密钥；Posh-SSH 回退可用密码）"
  }

  if (-not $useOpenSsh) {
    Write-Host '    OpenSSH 不可用，回退 Posh-SSH ...'
    Import-Module Posh-SSH -ErrorAction Stop
    $auth = @{ ComputerName = $VpsHost; AcceptKey = $true }
    if ($script:keyPath) {
      $auth['Credential'] = New-Object System.Management.Automation.PSCredential($User, (New-Object System.Security.SecureString))
      $auth['KeyFile'] = $script:keyPath
    } else {
      if ($env:VPS_PASS) { $sec = ConvertTo-SecureString $env:VPS_PASS -AsPlainText -Force }
      else { $sec = Read-Host "VPS 密码 ($User@$VpsHost)" -AsSecureString }
      $auth['Credential'] = New-Object System.Management.Automation.PSCredential($User, $sec)
    }
  } elseif ($passAuth) {
    throw '当前走 OpenSSH 通道，需提供 -KeyFile / $env:VPS_KEY（~/.ssh/hualing_deploy）'
  }

  Write-Host "[2/4] 上传部署包（通道: $(if ($useOpenSsh) {'OpenSSH'} else {'Posh-SSH'})；不覆盖远端 deploy.sh）..."
  # 远端 tar 名同样唯一：上传阶段不在 deploy.sh 的 flock 锁内，两条并发线同名上传会互相截断包体。
  # 远端执行针定的 /home/ubuntu/deploy.sh（chattr +i），客户端不再 scp 覆盖它。
  $remoteTarName = Split-Path $tar -Leaf
  if ($useOpenSsh) {
    $scpArgs = @('-o', 'StrictHostKeyChecking=accept-new', '-i', $script:keyPath)
    & $scpExe @scpArgs $tar "${User}@${VpsHost}:${RemoteDir}/$remoteTarName"
    if ($LASTEXITCODE -ne 0) { throw 'scp 上传 tar 失败' }
  } else {
    Set-SCPItem @auth -Path $tar -Destination $RemoteDir -Force
    $script:sshSession = New-SSHSession @auth -ConnectionTimeout 30
  }

  Write-Host '[3/4] 服务器侧原子部署 (针定 deploy.sh + 轮询日志) ...'
  try {
    # 日志名跟 tar 同后缀：并发线各写各的日志，后来者不再截断先行者正在写的 deploy.log
    # （撞 flock 锁的那条线只会在自己的日志里看到 ERROR，不污染别人的轮询判定）。
    $remoteLog = "deploy-$($remoteTarName -replace '\.tar\.gz$','').log"
    $launch = "cd $RemoteDir && nohup bash $RemoteDir/deploy.sh $RemoteDir/$remoteTarName >$remoteLog 2>&1 </dev/null & echo launched"
    Invoke-Remote $launch | Out-Null

    $deadline = (Get-Date).AddMinutes(15); $done = $false
    do {
      Start-Sleep -Seconds 8
      $p = Invoke-Remote "tail -4 $RemoteDir/$remoteLog 2>/dev/null; pgrep -f '[b]ash deploy.sh' >/dev/null && echo __RUN__ || echo __STOP__"
      Write-Host ('    ' + (($p -replace '__RUN__|__STOP__','').Trim() -replace "`n","`n    "))
      if ($p -match 'DONE @')                     { $done = $true; break }
      if ($p -match 'deploy ERROR|rolling back')  { throw "服务器部署失败(已尝试自动回滚)，详见服务器 $RemoteDir/$remoteLog" }
      if ($p -match '__STOP__') {
        $final = Invoke-Remote "tail -25 $RemoteDir/$remoteLog"
        if ($final -match 'DONE @') { $done = $true; break }
        throw "deploy.sh 已退出但未见 DONE，疑似中断：`n$final"
      }
    } until ((Get-Date) -gt $deadline)
    if (-not $done) { throw "部署轮询超时(>15min)，请上服务器查看 $RemoteDir/$remoteLog" }
    # 成功后清掉本次日志与历史遗留的唯一名日志（>2 天），避免 /home/ubuntu 堆积
    Invoke-Remote "rm -f $RemoteDir/$remoteLog; find $RemoteDir -maxdepth 1 -name 'deploy-*.log' -mtime +2 -delete 2>/dev/null; true" | Out-Null

    # [3.5/4] 差量同步发布物：源码包与服务器 rsync 都排除了这些目录，此处是唯一上载通道。
    # 远端同名同大小即跳过，只有新增/换版的安装包才真正走网络。
    if (-not $SkipAssets) {
      $appDir = "$RemoteDir/yuntech"
      $changed = 0
      $staleGuard = @()
      foreach ($rel in @('public/downloads', 'public/releases')) {
        $localDir = Join-Path $WebRoot ($rel -replace '/', '\')
        if (-not (Test-Path $localDir)) { continue }
        $files = @(Get-ChildItem $localDir -File -Recurse)
        if ($files.Count -eq 0) { continue }
        Write-Host "[3.5/4] 同步 $rel（$($files.Count) 个文件）..."
        foreach ($f in $files) {
          $sub = $f.FullName.Substring($localDir.Length).TrimStart('\') -replace '\\', '/'
          $remoteFile = "$appDir/$rel/$sub"
          $remoteParent = $remoteFile.Substring(0, $remoteFile.LastIndexOf('/'))
          # 变更判定分两档：大安装包比大小（快，几百 MB 不值当算哈希）；
          # 小元数据文件（manifest/latest.yml）比 MD5——换版本号后字节数常常不变
          # （版本/哈希/日期全定长），按大小判会被误跳过导致页面停在旧版本。
          # 发布指针单向守卫（2026-08-28 实锤事故）：public/downloads 的真相在**服务器**——
          # 打包线可能把新版发布到服务器/R2 而没回填本地树。此处原本无条件「本地覆盖远端」，
          # 于是一次纯官网内容部署把 latest.yml 从 1.0.58 打回 1.0.57，桌面自动更新当场指向旧包。
          # 现在对带版本号的指针文件比版本：远端更新 → 跳过并报警（内容部署照常完成，绝不回退生产）。
          if ($f.Name -in @('latest.yml', 'manifest.json')) {
            $localVer = Get-PublishVersion (Get-Content $f.FullName -Raw)
            $remoteVer = Get-PublishVersion (Invoke-Remote "cat '$remoteFile' 2>/dev/null || true")
            if ($localVer -and $remoteVer -and ([version]$remoteVer -gt [version]$localVer)) {
              Write-Host ("    ! {0}：远端 {1} 比本地 {2} 新 → 跳过上传（本地树落后）" -f $sub, $remoteVer, $localVer) -ForegroundColor Yellow
              $staleGuard += ("{0}（远端 {1} > 本地 {2}）" -f $sub, $remoteVer, $localVer)
              continue
            }
          }
          if ($f.Length -gt 1MB) {
            $sz = (Invoke-Remote "stat -c %s '$remoteFile' 2>/dev/null || echo 0").Trim()
            if ($sz -eq [string]$f.Length) {
              Write-Host ("    = {0}（远端同大小，跳过）" -f $sub)
              continue
            }
          } else {
            $localMd5 = (Get-FileHash -Algorithm MD5 -Path $f.FullName).Hash.ToLower()
            $remoteMd5 = (Invoke-Remote "md5sum '$remoteFile' 2>/dev/null | cut -d' ' -f1 || true").Trim()
            if ($remoteMd5 -eq $localMd5) {
              Write-Host ("    = {0}（远端同内容，跳过）" -f $sub)
              continue
            }
          }
          Write-Host ("    ^ {0}（{1:N1} MB）上传中..." -f $sub, ($f.Length / 1MB))
          Invoke-Remote "mkdir -p '$remoteParent'" | Out-Null
          if ($useOpenSsh) {
            & $scpExe @('-o', 'StrictHostKeyChecking=accept-new', '-i', $script:keyPath) $f.FullName "${User}@${VpsHost}:$remoteFile"
            if ($LASTEXITCODE -ne 0) { throw "scp 上传 $sub 失败" }
          } else {
            Set-SCPItem @auth -Path $f.FullName -Destination $remoteParent -Force
          }
          $changed++
        }
      }
      if ($staleGuard.Count -gt 0) {
        Write-Host ''
        Write-Warning ("发布指针未同步，已保留生产原值（官网内容部署不受影响）：`n  - " +
          ($staleGuard -join "`n  - ") +
          "`n  处理：请打包线把最新发布物（含 latest.yml / manifest.json）回填 website/public/downloads 后再部署。")
      }
      # Next.js 只在启动时建立 public/ 静态索引：新增文件后不重启会 404（本次事故的第二成因）。
      if ($changed -gt 0) {
        Write-Host "    发布物有 $changed 项变更 → 重启 pm2 让静态索引生效"
        # Q-14 #262：滚动 reload（cluster 模式）代替 restart，发布物生效不再有 5xx 窗
        Invoke-Remote "pm2 reload yuntech --update-env >/dev/null 2>&1 || pm2 restart yuntech --update-env >/dev/null 2>&1; sleep 4 && echo reloaded" | Out-Null
      }

      # [3.6/4] R2 镜像同步（下载提速 P0 · 2026-08-08）：官网 /dl 分流入口 R2 优先，
      # 发布物必须双源一致。有 rclone+凭证就同步（幂等，只传新增/变更），缺则提示不阻断
      # （/dl 对缺文件会自动回落本站，最多损失镜像加速，不会 404）。
      # conf 解析（117/176 双机对齐 2026-08-10）：env RCLONE_R2_CONF → 176 路径 → 单仓 deploy\secrets
      $r2conf = $env:RCLONE_R2_CONF
      if (-not $r2conf) {
        $r2conf = @(
          'C:\模仿音色\secrets\deploy\rclone_r2.conf',
          (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'deploy\secrets\rclone_r2.conf')
        ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
      }
      $rcloneCmd = (Get-Command rclone -ErrorAction SilentlyContinue).Source
      if (-not $rcloneCmd -and (Test-Path 'C:\tools\rclone\rclone.exe')) { $rcloneCmd = 'C:\tools\rclone\rclone.exe' }
      if ($rcloneCmd -and $r2conf) {
        Write-Host '[3.6/4] 同步发布物到 R2 镜像 ...'
        $pairs = @(
          @{ src = (Join-Path $WebRoot 'public\downloads'); dst = 'r2:avatarhub/downloads' },
          @{ src = (Join-Path $WebRoot 'public\releases');  dst = 'r2:avatarhub/releases' }
        )
        foreach ($p in $pairs) {
          if (-not (Test-Path $p.src)) { continue }
          & $rcloneCmd --config $r2conf copy $p.src $p.dst --transfers 4 --s3-chunk-size 32M --log-level ERROR
          if ($LASTEXITCODE -ne 0) { Write-Warning "R2 同步 $($p.src) 失败（/dl 会自动回落本站，事后可手动补：rclone copy ...）" }
          else { Write-Host "    [OK] $($p.src) → $($p.dst)" }
        }
      } else {
        Write-Host '    (跳过 R2 同步：本机无 rclone 或凭证；/dl 分流对缺文件会自动回落本站)'
      }
    }
  } finally {
    if (-not $useOpenSsh -and $script:sshSession) {
      Remove-SSHSession -SessionId $script:sshSession.SessionId | Out-Null
    }
  }

  Write-Host '[4/4] 公网体检 ...'
  $h = Invoke-RestMethod "$SiteUrl/api/health" -TimeoutSec 20
  Write-Host ("    healthy={0}  webhookSecret={1}  adminKey={2}  deepseek={3}" -f `
      $h.healthy, $h.checks.env.webhookSecret, $h.checks.env.adminKey, $h.checks.env.deepseekKey)
  if (-not $h.healthy) { throw '公网健康检查未通过' }
  Write-Host '部署完成 [OK]'
}
finally {
  Pop-Location
  if (Test-Path $tar) { Remove-Item $tar -Force -ErrorAction SilentlyContinue }
  if ($headStage -and (Test-Path $headStage)) {
    Remove-Item $headStage -Recurse -Force -ErrorAction SilentlyContinue
  }
}
