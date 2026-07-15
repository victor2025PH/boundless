# wujie/tools/repo_doctor.ps1 — 全域仓库体检（落地审计《架构检测说明》§4）。
# 只读、可重复跑。用法： powershell -File tools\repo_doctor.ps1
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot   # tools/ 的上一级 = 仓库根
if (-not (Test-Path (Join-Path $root '.git'))) { $root = 'D:\workspace\wujie' }
Set-Location $root
$fail = 0; $warn = 0
function Line($lvl, $msg) {
  if ($lvl -eq 'FAIL') { $script:fail++ } elseif ($lvl -eq 'WARN') { $script:warn++ }
  Write-Output ('[{0}] {1}' -f $lvl, $msg)
}

Write-Output '==================== wujie repo_doctor ===================='

# A 工作树
$dirty = (git status --porcelain | Measure-Object).Count
if ($dirty -eq 0) { Line 'OK' 'worktree clean' } else { Line 'WARN' ("worktree has $dirty uncommitted entries") }

# B 机密（真实密钥形态）
$sec = git grep -nIE "(sk-[A-Za-z0-9]{20,}|[0-9a-fA-F]{32}\.[A-Za-z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)" -- . ":(exclude)vendor/**" 2>$null
$secN = ($sec | Measure-Object).Count
if ($secN -eq 0) { Line 'OK' 'no real-secret patterns tracked' } else { Line 'FAIL' ("secret-like matches: $secN"); $sec | Select-Object -First 5 | ForEach-Object { Write-Output ('      ' + $_) } }

# C 违禁营销词（官网）
$ban = git grep -nIE "无审查|无禁区|uncensored" -- website 2>$null
$banN = ($ban | Measure-Object).Count
if ($banN -eq 0) { Line 'OK' 'website free of banned wording' } else { Line 'FAIL' ("banned wording: $banN") }

# D 依赖方向：platform 不得反向 import 产品/引擎/官网
$rev = git grep -nIE "from .*(engines|products|website)|require\(.*(engines|products|website)" -- platform 2>$null
$revN = ($rev | Measure-Object).Count
if ($revN -eq 0) { Line 'OK' 'platform has no reverse deps' } else { Line 'FAIL' ("platform reverse deps: $revN") }

# E 大文件（>10MB，LFS 指针不计）
$big = @()
git ls-files | ForEach-Object {
  $fp = Join-Path $root $_
  $fi = Get-Item -LiteralPath $fp -EA SilentlyContinue
  if ($fi -and $fi.Length -gt 10MB) { $big += ('{0:N1}MB {1}' -f ($fi.Length/1MB), $_) }
}
if ($big.Count -eq 0) { Line 'OK' 'no tracked file >10MB' } else { Line 'WARN' ("tracked files >10MB: " + $big.Count + ' (consider LFS)'); $big | ForEach-Object { Write-Output ('      ' + $_) } }

# F 冗余：重复站 / 浏览器缓存 应为 0
$dup = (git ls-files engines/chengjie/website | Measure-Object).Count
$pw = (git ls-files 'engines/avatarhub/demo_record/.pwprofile' | Measure-Object).Count
if ($dup -eq 0 -and $pw -eq 0) { Line 'OK' 'no stale dup-site / browser cache tracked' } else { Line 'WARN' ("dup-site=$dup pwcache=$pw still tracked") }

# G 完善度：platform 是否有实现
$plat = (git ls-files platform | Where-Object { $_ -notmatch 'README|gitkeep|MIGRATION|CONTRACT' } | Measure-Object).Count
if ($plat -gt 0) { Line 'OK' ("platform has $plat impl files") } else { Line 'WARN' 'platform still contract-only (compliance extraction staged for env machine)' }

# G2 products 清单门禁：每个产品必须有 product.yaml，含 brand_key/engine/compliance，且 engine 目录存在
$prodDirs = Get-ChildItem (Join-Path $root 'products') -Directory -EA SilentlyContinue
$bad = @()
foreach ($d in $prodDirs) {
  $y = Join-Path $d.FullName 'product.yaml'
  $okKeys = (Test-Path $y) -and (Select-String -LiteralPath $y -Pattern '^brand_key:' -Quiet) -and (Select-String -LiteralPath $y -Pattern '^engine:' -Quiet) -and (Select-String -LiteralPath $y -Pattern '^compliance:' -Quiet)
  $engOk = $true
  if (Test-Path $y) {
    $eng = (Select-String -LiteralPath $y -Pattern '^engine:\s*(\S+)').Matches[0].Groups[1].Value
    if ($eng) { $engOk = Test-Path (Join-Path $root ('engines/' + $eng)) }
  }
  if (-not ($okKeys -and $engOk)) { $bad += $d.Name }
}
$tot = ($prodDirs | Measure-Object).Count
if ($bad.Count -eq 0 -and $tot -gt 0) { Line 'OK' ("products: all $tot have valid product.yaml (brand_key/engine/compliance, engine exists)") }
else { Line 'WARN' ('products invalid/missing manifest: ' + ($bad -join ',')) }

# G3 提醒：仍是 TBD 占位的定价（非阻塞）
$tbd = (git grep -c "price: TBD" -- products 2>$null | Measure-Object).Count
if ($tbd -gt 0) { Line 'WARN' ("products with TBD pricing (owner to fill): $tbd file(s)") }

Write-Output '----------------------------------------------------------'
Write-Output ("SUMMARY: FAIL=$fail  WARN=$warn   tracked=" + ((git ls-files | Measure-Object).Count))
if ($fail -gt 0) { Write-Output 'DOCTOR: RED (fix FAILs before release)'; exit 1 }
elseif ($warn -gt 0) { Write-Output 'DOCTOR: AMBER (works; warnings are the completion backlog)' }
else { Write-Output 'DOCTOR: GREEN' }
Write-Output 'REPO_DOCTOR_DONE'
