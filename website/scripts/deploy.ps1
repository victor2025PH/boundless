<#
  华灵网站 · 本地一键部署 (Windows / PowerShell)
  流程: sync:brand → 打包 website/ → 上传(部署包 + deploy.sh) → 服务器侧原子部署 → 公网体检
  绝不在脚本中存放密码：从 $env:VPS_PASS 读取，缺失则安全提示输入(SecureString)。

  认证: 优先 SSH 密钥；找不到密钥文件时回退密码。
  传输: 优先本机 OpenSSH (scp/ssh)，无需 Posh-SSH；仅当 ssh 不可用时回退 Posh-SSH。

  用法:
    cd website
    pwsh ./scripts/deploy.ps1                     # 推荐（PowerShell 7+）
    ./scripts/deploy.ps1                          # Windows PS5 会自动改用 pwsh（若已安装）
    $env:VPS_PASS = '...'; ./scripts/deploy.ps1
#>
param(
  [string]$VpsHost      = $(if ($env:VPS_HOST) { $env:VPS_HOST } else { '165.154.233.121' }),
  [string]$User         = $(if ($env:VPS_USER) { $env:VPS_USER } else { 'ubuntu' }),
  [string]$RemoteDir    = '/home/ubuntu',
  [string]$SiteUrl      = $(if ($env:SITE_URL) { $env:SITE_URL } else { 'https://bd2026.cc' }),
  [string]$KeyFile      = $(if ($env:VPS_KEY) { $env:VPS_KEY } else { Join-Path $HOME '.ssh/hualing_deploy' }),
  [switch]$SkipAssets   # 只发源码，跳过 public/downloads、public/releases 发布物同步
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
    & $pwshCmd.Source @argList
    exit $LASTEXITCODE
  }
}

$WebRoot = Split-Path -Parent $PSScriptRoot
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

Push-Location $WebRoot
try {
  Write-Host '[0/4] sync:brand（vendored 品牌 preset，缺则服务器 next build 必挂）...'
  & node scripts/sync-brand.mjs
  if ($LASTEXITCODE -ne 0) { throw 'sync:brand 失败' }
  & node scripts/sync-brand.mjs --check
  if ($LASTEXITCODE -ne 0) { throw 'sync:brand --check 失败：vendor/brand 与 platform/brand 不一致' }
  Write-Host '    [OK] vendor/brand 已与上游对齐'

  Write-Host '[1/4] 打包 website/ (排除 node_modules/.next/.git/.env.local/临时文件) ...'
  if (Test-Path $tar) { Remove-Item $tar -Force }
  # public/downloads、public/releases（安装包等大文件）不进源码包：服务器侧 rsync 已 exclude
  # 令其常驻，由下方 [3.5/4] 差量上传，避免每次部署重传数百 MB。
  tar -czf $tar --exclude=node_modules "--exclude=.next*" --exclude=.git --exclude=.env.local `
      --exclude=.hand-shots --exclude=.robot-shots `
      "--exclude=*.tsbuildinfo" "--exclude=*.log" --exclude=og-test.png --exclude=test-fill.png `
      --exclude=ops-overlay.tgz "--exclude=scripts/_*" `
      --exclude=public/downloads --exclude=public/releases .
  if ($LASTEXITCODE -ne 0) { throw '打包失败' }
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

  Write-Host "[2/4] 上传部署包 + deploy.sh（通道: $(if ($useOpenSsh) {'OpenSSH'} else {'Posh-SSH'})）..."
  # 远端 tar 名同样唯一：上传阶段不在 deploy.sh 的 flock 锁内，两条并发线同名上传会互相截断包体。
  # deploy.sh 收 $1 指定包路径、部署完成后自行 rm，唯一名不会堆积。
  $remoteTarName = Split-Path $tar -Leaf
  if ($useOpenSsh) {
    $scpArgs = @('-o', 'StrictHostKeyChecking=accept-new', '-i', $script:keyPath)
    & $scpExe @scpArgs $tar "${User}@${VpsHost}:${RemoteDir}/$remoteTarName"
    if ($LASTEXITCODE -ne 0) { throw 'scp 上传 tar 失败' }
    & $scpExe @scpArgs (Join-Path $PSScriptRoot 'deploy.sh') "${User}@${VpsHost}:${RemoteDir}/deploy.sh"
    if ($LASTEXITCODE -ne 0) { throw 'scp 上传 deploy.sh 失败' }
  } else {
    Set-SCPItem @auth -Path $tar -Destination $RemoteDir -Force
    Set-SCPItem @auth -Path (Join-Path $PSScriptRoot 'deploy.sh') -Destination $RemoteDir -Force
    $script:sshSession = New-SSHSession @auth -ConnectionTimeout 30
  }

  Write-Host '[3/4] 服务器侧原子部署 (后台执行 deploy.sh + 轮询日志) ...'
  try {
    # 日志名跟 tar 同后缀：并发线各写各的日志，后来者不再截断先行者正在写的 deploy.log
    # （撞 flock 锁的那条线只会在自己的日志里看到 ERROR，不污染别人的轮询判定）。
    $remoteLog = "deploy-$($remoteTarName -replace '\.tar\.gz$','').log"
    $launch = "sed -i 's/\r$//' $RemoteDir/deploy.sh; cd $RemoteDir && nohup bash deploy.sh $RemoteDir/$remoteTarName >$remoteLog 2>&1 </dev/null & echo launched"
    Invoke-Remote $launch | Out-Null

    $deadline = (Get-Date).AddMinutes(10); $done = $false
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
    if (-not $done) { throw "部署轮询超时(>10min)，请上服务器查看 $RemoteDir/$remoteLog" }
    # 成功后清掉本次日志与历史遗留的唯一名日志（>2 天），避免 /home/ubuntu 堆积
    Invoke-Remote "rm -f $RemoteDir/$remoteLog; find $RemoteDir -maxdepth 1 -name 'deploy-*.log' -mtime +2 -delete 2>/dev/null; true" | Out-Null

    # [3.5/4] 差量同步发布物：源码包与服务器 rsync 都排除了这些目录，此处是唯一上载通道。
    # 远端同名同大小即跳过，只有新增/换版的安装包才真正走网络。
    if (-not $SkipAssets) {
      $appDir = "$RemoteDir/yuntech"
      $changed = 0
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
          $sz = (Invoke-Remote "stat -c %s '$remoteFile' 2>/dev/null || echo 0").Trim()
          if ($sz -eq [string]$f.Length) {
            Write-Host ("    = {0}（远端同大小，跳过）" -f $sub)
            continue
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
      # Next.js 只在启动时建立 public/ 静态索引：新增文件后不重启会 404（本次事故的第二成因）。
      if ($changed -gt 0) {
        Write-Host "    发布物有 $changed 项变更 → 重启 pm2 让静态索引生效"
        Invoke-Remote "pm2 restart yuntech --update-env >/dev/null 2>&1 && sleep 4 && echo restarted" | Out-Null
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
}
