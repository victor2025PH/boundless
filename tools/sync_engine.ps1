<#
  sync_engine.ps1 — 把各引擎『在别处持续开发的最新净码』拉进 boundless/engines/<engine>。
  为什么不是 git subtree/submodule：见 tools/engine_sources.json 头注（会把上游运行时/机密/大权重带进干净单仓）。
  本工具做『净码再同步』：过滤复制上游源码 -> 人工 review -> 作为普通 boundless 提交。

  用法：
    powershell -File tools\sync_engine.ps1 -Engine avatarhub -Check     # 只读：上游有没有新码可拉？
    powershell -File tools\sync_engine.ps1 -Engine avatarhub            # 干跑：列出会新增/变更的源码文件(不改)
    powershell -File tools\sync_engine.ps1 -Engine avatarhub -From "\\DEVPC\share\模仿音色"  # 指定直连源(上游未 push 时)
    powershell -File tools\sync_engine.ps1 -Engine avatarhub -Apply     # 真同步(仅新增/覆盖，不删；孤儿文件另行报告)
    powershell -File tools\sync_engine.ps1 -Engine all -Check

  安全：默认干跑；-Apply 才写；永不碰上游的运行时/机密/大文件；永不 git-merge 上游历史。
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$Engine,
  [string]$From = '',
  [string]$Ref = '',
  [switch]$Check,
  [switch]$Apply
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$ToolsDir = $PSScriptRoot
$Root = Split-Path -Parent $ToolsDir
$ManifestPath = Join-Path $ToolsDir 'engine_sources.json'
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

$targets = @()
if ($Engine -eq 'all') { $targets = @($Manifest.engines.PSObject.Properties.Name) }
elseif ($Manifest.engines.PSObject.Properties.Name -contains $Engine) { $targets = @($Engine) }
else { Write-Error "未知引擎 '$Engine'。可选: $($Manifest.engines.PSObject.Properties.Name -join ', '), all"; exit 3 }

function Probe-Upstream($name, $cfg) {
  Write-Host ""
  Write-Host ("===== [$name] $($cfg.title) =====") -ForegroundColor Cyan
  Write-Host ("  上游 git : {0} ({1})" -f $cfg.upstream_git, $cfg.branch)
  Write-Host ("  上次同步 : {0} [{1}] {2}" -f $cfg.last_synced.ref, $cfg.last_synced.kind, $cfg.last_synced.note) -ForegroundColor DarkGray
  $env:GIT_TERMINAL_PROMPT = '0'
  $remoteSha = ''
  try {
    $line = git ls-remote --heads $cfg.upstream_git $cfg.branch 2>$null | Select-Object -First 1
    if ($line) { $remoteSha = ($line -split '\s+')[0] }
  } catch {}
  if (-not $remoteSha) {
    Write-Host "  [!] 上游 git 不可达(网络/权限)。请改用 -From 指向直连源。" -ForegroundColor Yellow
  } else {
    Write-Host ("  上游 GitHub HEAD({0}) = {1}" -f $cfg.branch, $remoteSha.Substring(0,10))
    $baseKind = $cfg.last_synced.kind
    if ($baseKind -eq 'migration-snapshot') {
      Write-Host "  [判断] 上次是『迁移快照』(取自开发机较新副本)。若上游 GitHub HEAD 不比它新，" -ForegroundColor Yellow
      Write-Host "         说明开发机的新代码还没 push 到 GitHub —— 要 boundless 能拉，请先让开发机 push，" -ForegroundColor Yellow
      Write-Host "         或用 -From 直连开发机共享目录/SSH 同步。" -ForegroundColor Yellow
    }
    if ($name -eq 'avatarhub') {
      Write-Host "  [事实] 本机 D:/faceX/mfys 与 GitHub 同为 6-24 旧提交；换脸/换声最新码在开发机本地(未 push)。" -ForegroundColor Yellow
    }
  }
}

function Resolve-Source($name, $cfg) {
  if ($From) {
    if (-not (Test-Path $From)) { Write-Host "  [ERR] -From 路径不存在: $From" -ForegroundColor Red; return $null }
    Write-Host ("  源(直连 -From): {0}" -f $From) -ForegroundColor Green
    return $From
  }
  # 无 -From：用本机 local_mirror（可能过期，给出提示）
  if ($cfg.local_mirror -and (Test-Path $cfg.local_mirror)) {
    Write-Host ("  源(本机 local_mirror): {0}" -f $cfg.local_mirror) -ForegroundColor DarkYellow
    Write-Host "  [注] 未 -From：用本机镜像。若要开发机最新码，请 -From 直连开发机，或先让开发机 push 后从 git clone 再 -From。" -ForegroundColor DarkGray
    return $cfg.local_mirror
  }
  Write-Host "  [ERR] 无可用源：上游未 clone 到本机，也未给 -From。" -ForegroundColor Red
  Write-Host ("        建议：git clone {0} <tmp> 后 -From <tmp>；或 -From 直连开发机路径。" -f $cfg.upstream_git) -ForegroundColor DarkGray
  return $null
}

function Test-Excluded($rel, $cfg) {
  # 安全网：即便上游 git 跟踪了机密/大文件，也按清单再滤一层
  $segs = $rel -replace '/', '\' -split '\\'
  foreach ($d in $cfg.exclude_dirs) { if ($segs -contains $d) { return $true } }
  $leaf = $segs[-1]
  foreach ($f in $cfg.exclude_files) { if ($leaf -like $f) { return $true } }
  return $false
}

function Get-CleanFileList($src, $cfg) {
  # 关键优化：只同步『上游 git 的净码』(tracked + 未忽略的 untracked)，
  # 而不是整棵脏工作树 —— 天然排除 build/tmp/日志/权重/机密(它们本就 gitignore)。
  $isGit = Test-Path (Join-Path $src '.git')
  if (-not $isGit) {
    Write-Host "  [注] 源不是 git 仓：回退『整树 - 排除清单』(best-effort，可能含少量非源文件)。" -ForegroundColor DarkYellow
    $all = Get-ChildItem $src -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName.Substring($src.Length).TrimStart('\','/') -replace '\\','/' }
    return @{ git = $false; files = @($all | Where-Object { -not (Test-Excluded $_ $cfg) }) }
  }
  $tracked  = @(git -C $src ls-files 2>$null)
  $untracked = @(git -C $src ls-files --others --exclude-standard 2>$null)
  $union = @($tracked + $untracked) | Sort-Object -Unique
  $clean = @($union | Where-Object { $_ -and -not (Test-Excluded $_ $cfg) })
  return @{ git = $true; files = $clean }
}

function Sync-One($name, $cfg) {
  Write-Host ""
  Write-Host ("===== 同步 [$name] $($cfg.title) =====") -ForegroundColor Cyan
  $src = Resolve-Source $name $cfg
  if (-not $src) { return }
  $dst = Join-Path $Root ("engines/" + $name)
  if (-not (Test-Path $dst)) { Write-Host "  [ERR] 目标不存在: $dst" -ForegroundColor Red; return }

  $res = Get-CleanFileList $src $cfg
  $files = $res.files
  $srcKind = if ($res.git) { 'git 净码(tracked+未忽略)' } else { '整树-排除清单' }
  Write-Host ("  源净码文件数: {0}  ({1})" -f $files.Count, $srcKind)

  $new = New-Object System.Collections.Generic.List[string]
  $changed = New-Object System.Collections.Generic.List[string]
  foreach ($rel in $files) {
    $sp = Join-Path $src ($rel -replace '/', '\')
    $dp = Join-Path $dst ($rel -replace '/', '\')
    if (-not (Test-Path -LiteralPath $sp -PathType Leaf)) { continue }
    if (-not (Test-Path -LiteralPath $dp -PathType Leaf)) { $new.Add($rel); continue }
    $ls = (Get-Item -LiteralPath $sp).Length; $ld = (Get-Item -LiteralPath $dp).Length
    if ($ls -ne $ld) { $changed.Add($rel) }
    elseif ((Get-FileHash -LiteralPath $sp).Hash -ne (Get-FileHash -LiteralPath $dp).Hash) { $changed.Add($rel) }
  }
  $mode = if ($Apply) { 'APPLY(写入)' } else { 'DRY(仅列出，未改)' }
  Write-Host ("  模式: {0}" -f $mode) -ForegroundColor $(if ($Apply) {'Green'} else {'Cyan'})
  Write-Host ("  新增: {0}   变更: {1}" -f $new.Count, $changed.Count)
  $sample = @($new + $changed) | Select-Object -First 30
  foreach ($s in $sample) { Write-Host ("    " + $s) -ForegroundColor DarkGray }
  if (($new.Count + $changed.Count) -gt 30) { Write-Host ("    ... 其余 " + ($new.Count + $changed.Count - 30) + " 个") -ForegroundColor DarkGray }

  if ($Apply) {
    $copied = 0
    foreach ($rel in @($new + $changed)) {
      $sp = Join-Path $src ($rel -replace '/', '\'); $dp = Join-Path $dst ($rel -replace '/', '\')
      $dd = Split-Path $dp -Parent
      if (-not (Test-Path $dd)) { New-Item -ItemType Directory -Force -Path $dd | Out-Null }
      Copy-Item -LiteralPath $sp -Destination $dp -Force; $copied++
    }
    Write-Host ("  [APPLY] 已写入 {0} 个文件。请 review 后提交：" -f $copied) -ForegroundColor Green
    Write-Host ("     git -C `"{0}`" status --short engines/{1}" -f $Root, $name) -ForegroundColor DarkGray
    Write-Host ("     git -C `"{0}`" add engines/{1}; git commit -m 'chore(sync): {1} <- upstream 净码'" -f $Root, $name) -ForegroundColor DarkGray
    Write-Host "  注：只新增/覆盖，不删除；上游已删的文件需人工确认后手动删(真实差异以 git status 为准)。" -ForegroundColor DarkGray
  } else {
    Write-Host "  -> 确认无误后加 -Apply 真正写入。真实变更以 apply 后 git status 为准。" -ForegroundColor DarkGray
  }
}

foreach ($t in $targets) {
  $cfg = $Manifest.engines.$t
  if ($Check) { Probe-Upstream $t $cfg } else { Sync-One $t $cfg }
}
Write-Host ""
Write-Host "sync_engine 完成。" -ForegroundColor Green
