<#
  智控 MatrixX 一键发布脚本（跨 tgkz2026 客户端 + boundless/website 官网）
  ============================================================================
  一条命令完成：打包客户端 → 算 SHA → 回填官网 → 上传安装包 → 部署官网 → 线上体检 → 更新链路验证。

  流程（每个阶段可单独跳过，失败即停）：
    [0] 版本自增      -BumpPatch / -BumpMinor 改 tgkz2026/package.json（打包前）
    [1] 客户端打包    tgkz2026: npm run dist:win               (-SkipClientBuild 用现有 release/)
    [2] 版本 & 校验    latest.yml 版本 + exe SHA-256 + sha512 与 yml 一致
    [3] 回填官网      website/lib/matrixxContent.ts (version/filename/url/sha256)
    [4] 上传安装包    scp release/{exe,blockmap,latest.yml} → /var/www/dl/releases/matrixx/  (-SkipUpload)
    [5] 部署官网      sync:brand(+--check) + 打包 + 远端 deploy.sh（含 vendor/brand 门禁）(-SkipWebDeploy)
    [6] 线上体检      latest.yml 版本/sha512 / exe 头 / 首页八产品 / 下载页含新版本
    [7] 更新链路验证  无头 electron 伪装低版本 checkForUpdates 发现线上新版本         (-SkipUpdaterCheck)

  用法（在 D:\workspace\boundless 下）：
    ./scripts/release-matrixx.ps1                              # 完整发布
    ./scripts/release-matrixx.ps1 -BumpPatch                   # patch+1 后完整发布（2.1.1→2.1.2）
    ./scripts/release-matrixx.ps1 -BumpMinor                   # minor+1、patch归零（2.1.1→2.2.0）
    ./scripts/release-matrixx.ps1 -SkipClientBuild             # 用现有安装包发布
    ./scripts/release-matrixx.ps1 -SkipClientBuild -SkipUpload # 只重新部署官网 + 体检 + 更新验证
    ./scripts/release-matrixx.ps1 -HealthOnly                  # 只跑线上体检 + 更新链路验证

  认证：SSH 密钥 ~/.ssh/hualing_deploy（deploy/ssh_config.boundless 的 vps-bd2026）。
  设计取舍：官网部署不走 website/scripts/deploy.ps1（其 Posh-SSH 后台启动命令会 EndExecute 超时），
            改用 OpenSSH 直连 + 轮询服务器 deploy.log，更稳、更可观测；服务器侧仍复用既有 deploy.sh。
#>
[CmdletBinding()]
param(
  [string]$Root = '',
  [string]$VpsHost      = $(if ($env:VPS_HOST) { $env:VPS_HOST } else { '165.154.233.121' }),
  [string]$User         = $(if ($env:VPS_USER) { $env:VPS_USER } else { 'ubuntu' }),
  [string]$KeyFile      = $(if ($env:VPS_KEY)  { $env:VPS_KEY }  else { (Join-Path $HOME '.ssh/hualing_deploy') }),
  [string]$SiteUrl      = 'https://bd2026.cc',
  [string]$RelDir       = '/var/www/dl/releases/matrixx',
  [string]$RemoteHome   = '/home/ubuntu',
  [switch]$SkipClientBuild,
  [switch]$SkipUpload,
  [switch]$SkipWebDeploy,
  [switch]$SkipUpdaterCheck,
  [switch]$BumpPatch,
  [switch]$BumpMinor,
  [switch]$HealthOnly
)

$ErrorActionPreference = 'Stop'
# 参数默认值里不要用 $PSScriptRoot：少数调用方式下 param 求值时仍为空
if (-not $Root) {
  $here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
  $Root = Split-Path -Parent $here
}
$Client = Join-Path $Root 'tgkz2026'
$Web    = Join-Path $Root 'website'
$SshKey = (Resolve-Path $KeyFile).Path
$SSH  = @('-i', $SshKey, '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=accept-new')
$Dest = "$User@$VpsHost"

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)       { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Die($msg)      { Write-Host "    [FAIL] $msg" -ForegroundColor Red; throw $msg }
function Ssh([string]$cmd) { & ssh @SSH $Dest $cmd }

# latest.yml 常被当成 application/octet-stream，Invoke-WebRequest 的 .Content 变成 byte[]；
# 再经 PowerShell 管道/字符串化会变成 "118 101 114 ..."，正则全挂。统一用 WebClient 拉 UTF-8 文本。
function Get-TextUrl([string]$url, [int]$TimeoutSec = 20) {
  $wc = New-Object System.Net.WebClient
  try {
    $wc.Encoding = [System.Text.Encoding]::UTF8
    # WebClient 无细粒度 Timeout；对小文件足够。失败由外层 ErrorAction Stop 抛出。
    return $wc.DownloadString($url)
  } finally {
    $wc.Dispose()
  }
}

# ---------------- 自动更新链路验证（阶段7）----------------
# 无头 electron 跑 electron-updater：默认伪装 0.0.1，连线上 feed，
# 确认能发现 $ver 且解析出正确安装包 URL（零下载、零安装）。环境变量驱动 verify-updater.js。
function Invoke-UpdaterCheck([string]$ver) {
  if ($SkipUpdaterCheck) { Step 7 "自动更新链路验证 (跳过)"; return }
  Step 7 "自动更新链路验证 (无头 electron checkForUpdates，伪装低版本)"
  $el = Join-Path $Client 'node_modules\.bin\electron.cmd'
  if (-not (Test-Path $el)) { Write-Host "    [skip] 未找到 electron，跳过更新链路验证"; return }
  Push-Location $Client
  try {
    $env:MATRIXX_EXPECT_VERSION = $ver
    $env:MATRIXX_FEED_URL       = "$SiteUrl/releases/matrixx/"
    $env:MATRIXX_FAKE_VERSION   = '0.0.1'
    & $el scripts\verify-updater.js
    $code = $LASTEXITCODE
    Remove-Item Env:\MATRIXX_EXPECT_VERSION, Env:\MATRIXX_FEED_URL, Env:\MATRIXX_FAKE_VERSION -ErrorAction SilentlyContinue
    if ($code -ne 0) { Die "自动更新链路验证失败 (exit $code)" }
  } finally { Pop-Location }
  Ok "旧版本可检测到线上 v$ver（自动更新链路通）"
}

# ---------------- 体检（阶段6，也可 -HealthOnly 单独跑）----------------
function Invoke-Health([string]$ver, [string]$exe) {
  Step 6 "线上体检"
  $y = Get-TextUrl "$SiteUrl/releases/matrixx/latest.yml"
  $onlineVer = ([regex]::Match($y, 'version:\s*([0-9.]+)')).Groups[1].Value
  if ($onlineVer -ne $ver) { Die "latest.yml 版本 $onlineVer != 期望 $ver" }
  Ok "latest.yml version=$onlineVer"

  $ymlPath = ([regex]::Match($y, 'path:\s*(\S+)')).Groups[1].Value
  $ymlSha512 = ([regex]::Match($y, 'sha512:\s*(\S+)')).Groups[1].Value
  if ($ymlPath -ne $exe) { Die "latest.yml path=$ymlPath != 期望 $exe" }
  if (-not $ymlSha512 -or $ymlSha512.Length -lt 40) { Die "latest.yml 缺少有效 sha512" }
  Ok "latest.yml path/sha512 齐全"

  $r = Invoke-WebRequest "$SiteUrl/releases/matrixx/$exe" -Method Head -UseBasicParsing -TimeoutSec 30
  if ($r.StatusCode -ne 200) { Die "安装包 HEAD=$($r.StatusCode)" }
  Ok "安装包可下载 200  len=$($r.Headers.'Content-Length')"

  # 本地仍有 release/ 时，用 electron-updater 同款 sha512 再对一次（不下载 500MB）
  $localExe = Join-Path $Client "release\$exe"
  if (Test-Path $localExe) {
    Push-Location $Client
    try {
      & node scripts\verify-release-integrity.mjs
      if ($LASTEXITCODE -ne 0) { Die "本地 release sha512 与 latest.yml 不一致" }
    } finally { Pop-Location }
    Ok "本地 sha512 ↔ latest.yml 一致（electron-updater 摘要）"
  }

  $homeHtml = (Invoke-WebRequest "$SiteUrl/" -UseBasicParsing -TimeoutSec 20).Content
  if ($homeHtml -notmatch '八款产品') { Die "首页未出现『八款产品』" }
  Ok "首页口径=八款产品"

  $dl = (Invoke-WebRequest "$SiteUrl/matrix/download" -UseBasicParsing -TimeoutSec 20).Content
  if ($dl -notmatch [regex]::Escape($exe)) { Die "下载页未含 $exe" }
  if ($dl -notmatch [regex]::Escape($ver)) { Die "下载页未含版本 $ver" }
  Ok "下载页含 $exe / v$ver"
  Write-Host "`n[发布体检通过] $SiteUrl/matrix/download 已是 v$ver" -ForegroundColor Green
}

# 若只体检：从线上 latest.yml 反推版本
if ($HealthOnly) {
  $y = Get-TextUrl "$SiteUrl/releases/matrixx/latest.yml"
  $ver = ([regex]::Match($y, 'version:\s*([0-9.]+)')).Groups[1].Value
  Invoke-Health $ver "MatrixX-$ver-Setup.exe"
  Invoke-UpdaterCheck $ver
  return
}

# ---------------- [0] 版本自增（可选，打包前改 tgkz2026/package.json）----------------
if ($BumpPatch -or $BumpMinor) {
  if ($SkipClientBuild) { Die "-BumpPatch/-BumpMinor 需重新打包，不能与 -SkipClientBuild 同用" }
  Step 0 "版本自增 (tgkz2026/package.json)"
  $pkgPath = Join-Path $Client 'package.json'
  # 用 .NET 读 UTF8（无 BOM），避免 PowerShell Get-Content 对编码的歧义
  $pkg = [System.IO.File]::ReadAllText($pkgPath)
  $cur = ([regex]::Match($pkg, '"version":\s*"([0-9]+)\.([0-9]+)\.([0-9]+)"'))
  if (-not $cur.Success) { Die "无法解析 package.json 版本号" }
  $mj = [int]$cur.Groups[1].Value; $mn = [int]$cur.Groups[2].Value; $pt = [int]$cur.Groups[3].Value
  if ($BumpMinor) { $mn += 1; $pt = 0 } else { $pt += 1 }
  $newVer = "$mj.$mn.$pt"
  $pkg = [regex]::Replace($pkg, '("version":\s*")[0-9]+\.[0-9]+\.[0-9]+(")', ('${1}' + $newVer + '${2}'), 1)
  # 无 BOM 写回，避免 npm/electron-builder 解析 package.json 异常
  $utf8NoBom = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($pkgPath, $pkg, $utf8NoBom)
  Ok "版本 $($cur.Groups[1].Value).$($cur.Groups[2].Value).$($cur.Groups[3].Value) → $newVer"
}

# ---------------- [1] 客户端打包 ----------------
if (-not $SkipClientBuild) {
  Step 1 "客户端打包 (tgkz2026: npm run dist:win)"
  Push-Location $Client
  try {
    Get-Process matrixx-backend, electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    & npm run dist:win
    if ($LASTEXITCODE -ne 0) { Die "客户端打包失败" }
  } finally { Pop-Location }
  Ok "客户端打包完成"
} else { Step 1 "客户端打包 (跳过，用现有 release/)" }

# ---------------- [2] 版本 & 校验 ----------------
Step 2 "读取版本并计算 SHA-256"
$relDirLocal = Join-Path $Client 'release'
$latest = Join-Path $relDirLocal 'latest.yml'
if (-not (Test-Path $latest)) { Die "找不到 $latest（先打包或去掉 -SkipClientBuild）" }
$ly = Get-Content $latest -Raw
$Ver = ([regex]::Match($ly, 'version:\s*([0-9.]+)')).Groups[1].Value
if (-not $Ver) { Die "无法从 latest.yml 解析版本" }
$Exe = "MatrixX-$Ver-Setup.exe"
$ExePath = Join-Path $relDirLocal $Exe
if (-not (Test-Path $ExePath)) { Die "找不到安装包 $ExePath" }
$Sha256 = (Get-FileHash $ExePath -Algorithm SHA256).Hash.ToLower()
Ok "版本 v$Ver  文件 $Exe  SHA256 $Sha256"
Push-Location $Client
try {
  & node scripts\verify-release-integrity.mjs
  if ($LASTEXITCODE -ne 0) { Die "release sha512 与 latest.yml 不一致（打包产物损坏？）" }
} finally { Pop-Location }
Ok "sha512 ↔ latest.yml 一致"

# ---------------- [3] 回填官网数据源 ----------------
Step 3 "回填 website/lib/matrixxContent.ts"
$mc = Join-Path $Web 'lib/matrixxContent.ts'
$txt = Get-Content $mc -Raw
$txt = [regex]::Replace($txt, 'version:\s*"[^"]*"',  ('version: "{0}"' -f $Ver), 1)
$txt = [regex]::Replace($txt, 'filename:\s*"[^"]*"', ('filename: "{0}"' -f $Exe), 1)
$txt = [regex]::Replace($txt, 'sha256:\s*"[^"]*"',   ('sha256: "{0}"' -f $Sha256), 1)
$txt = [regex]::Replace($txt, '\$\{MATRIXX_RELEASE_BASE\}/MatrixX-[0-9.]+-Setup\.exe', ('${MATRIXX_RELEASE_BASE}/' + $Exe))
Set-Content $mc $txt -NoNewline -Encoding UTF8
Ok "已回填 version/filename/url/sha256"

# ---------------- [4] 上传安装包 ----------------
if (-not $SkipUpload) {
  Step 4 "上传安装包到 $RelDir"
  $bmap = "$ExePath.blockmap"
  & scp @SSH $latest "$Dest`:$RemoteHome/" ; if ($LASTEXITCODE -ne 0) { Die "scp latest.yml 失败" }
  if (Test-Path $bmap) { & scp @SSH $bmap "$Dest`:$RemoteHome/"; if ($LASTEXITCODE -ne 0) { Die "scp blockmap 失败" } }
  Write-Host "    上传安装包（约 507MB，稍候）..."
  & scp @SSH $ExePath "$Dest`:$RemoteHome/" ; if ($LASTEXITCODE -ne 0) { Die "scp exe 失败" }
  $place = "sudo mkdir -p $RelDir && sudo mv $RemoteHome/$Exe $RemoteHome/$Exe.blockmap $RemoteHome/latest.yml $RelDir/ && sudo chown root:root $RelDir/* && sudo chmod 644 $RelDir/* && sha256sum $RelDir/$Exe"
  $out = Ssh $place
  Write-Host ("    远端就位: " + ($out -join ' '))
  if (($out -join ' ') -notmatch $Sha256) { Die "远端 SHA256 与本地不一致！" }
  Ok "安装包就位且 SHA256 一致"
} else { Step 4 "上传安装包 (跳过)" }

# ---------------- [5] 部署官网 ----------------
if (-not $SkipWebDeploy) {
  Step 5 "部署官网 (sync:brand --check + 打包 + 远端 deploy.sh)"
  Push-Location $Web
  try {
    & node scripts/sync-brand.mjs
    if ($LASTEXITCODE -ne 0) { Die "sync:brand 失败" }
    & node scripts/sync-brand.mjs --check
    if ($LASTEXITCODE -ne 0) { Die "sync:brand --check 失败：vendor/brand 与 platform 不一致" }
    Ok "vendor/brand 已与 platform/brand 对齐"
    $tar = Join-Path $env:TEMP 'website-deploy.tar.gz'
    if (Test-Path $tar) { Remove-Item $tar -Force }
    tar -czf $tar --exclude=node_modules --exclude='.next' --exclude='.next-*' --exclude=.git `
        --exclude=.env.local "--exclude=*.tsbuildinfo" "--exclude=*.log" --exclude=ops-overlay.tgz `
        "--exclude=scripts/_*" --exclude=_matrix-shots .
    if ($LASTEXITCODE -ne 0) { Die "打包 website 失败" }
    Ok ("包大小 {0:N1} MB" -f ((Get-Item $tar).Length / 1MB))
    & scp @SSH $tar "$Dest`:$RemoteHome/website-deploy.tar.gz"; if ($LASTEXITCODE -ne 0) { Die "上传 website 包失败" }
    & scp @SSH (Join-Path $Web 'scripts/deploy.sh') "$Dest`:$RemoteHome/deploy.sh"; if ($LASTEXITCODE -ne 0) { Die "上传 deploy.sh 失败" }

    # OpenSSH 直连后台启动 + 轮询 deploy.log（避开 Posh-SSH 后台命令 EndExecute 超时坑）
    Ssh "cd $RemoteHome && sed -i 's/\r`$//' deploy.sh && rm -f deploy.log && nohup bash deploy.sh $RemoteHome/website-deploy.tar.gz >deploy.log 2>&1 </dev/null & echo launched" | Out-Null
    $deadline = (Get-Date).AddMinutes(10); $done = $false
    do {
      Start-Sleep -Seconds 8
      $p = (Ssh "tail -3 $RemoteHome/deploy.log 2>/dev/null; pgrep -f '[b]ash deploy.sh' >/dev/null && echo __RUN__ || echo __STOP__") -join "`n"
      $show = ($p -replace '__RUN__|__STOP__','').Trim()
      if ($show) { Write-Host ("    " + ($show -replace "`n","`n    ")) }
      if ($p -match 'DONE @') { $done = $true; break }
      if ($p -match 'deploy ERROR|rolling back') { Die "服务器部署失败(已回滚)，见 $RemoteHome/deploy.log" }
      if ($p -match '__STOP__') {
        $final = (Ssh "tail -20 $RemoteHome/deploy.log") -join "`n"
        if ($final -match 'DONE @') { $done = $true; break }
        Die "deploy.sh 已退出但未见 DONE：`n$final"
      }
    } until ((Get-Date) -gt $deadline)
    if (-not $done) { Die "部署轮询超时(>10min)" }
    Ok "官网部署完成 (DONE)"
  } finally { Pop-Location }
} else { Step 5 "部署官网 (跳过)" }

# ---------------- [6] 体检 + [7] 更新链路验证 ----------------
Invoke-Health $Ver $Exe
Invoke-UpdaterCheck $Ver
Write-Host "`n===== 发布完成 v$Ver =====" -ForegroundColor Green
