# boundless/tools/repo_doctor.ps1 — 全域仓库体检（落地审计《架构检测说明》§4）。
# 只读、可重复跑。用法： powershell -File tools\repo_doctor.ps1
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot   # tools/ 的上一级 = 仓库根（与文件夹名无关）
if (-not (Test-Path (Join-Path $root '.git'))) { $root = (Get-Location).Path }
Set-Location $root
$fail = 0; $warn = 0
function Line($lvl, $msg) {
  if ($lvl -eq 'FAIL') { $script:fail++ } elseif ($lvl -eq 'WARN') { $script:warn++ }
  Write-Output ('[{0}] {1}' -f $lvl, $msg)
}

Write-Output '==================== boundless repo_doctor ===================='

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

# H 官网产品口径门禁：禁止硬编码「五/六/七 大|条 产品」类数字（白名单 brand.ts）
$copyHits = git grep -nIE "([五六七八]|[5678])\s*(大|条)\s*产品|([Ff]ive|[Ss]ix|[Ss]even)\s+(AI\s+)?[Pp]roduct|[Ff]ive\s+walls" -- website 2>$null |
  Where-Object { $_ -notmatch 'website/lib/brand\.ts' -and $_ -notmatch 'node_modules|\.next|package-lock' }
$copyN = ($copyHits | Measure-Object).Count
if ($copyN -eq 0) { Line 'OK' 'website product-count copy uses PRODUCT_COUNT (no hard-coded 5/6/7)' }
else {
  # 全站已清零后升级为 FAIL：新硬编码数字会在 pre-push 被拦下（白名单 brand.ts）
  Line 'FAIL' ("hard-coded product-count copy: $copyN (use PRODUCT_COUNT / FAMILY_PITCH from lib/brand.ts)")
  $copyHits | Select-Object -First 8 | ForEach-Object { Write-Output ('      ' + $_) }
}

# I 产品图标完整性：7 张必须存在且为正方形（防再混入非方/缺 voxx）
$iconKeys = @('reachx','chatx','facex','voicex','livex','lingox','voxx')
$iconBad = @()
Add-Type -AssemblyName System.Drawing -EA SilentlyContinue
foreach ($k in $iconKeys) {
  $fp = Join-Path $root ("website/public/brand/products/$k.png")
  if (-not (Test-Path $fp)) { $iconBad += "$k missing"; continue }
  try {
    $img = [System.Drawing.Image]::FromFile((Resolve-Path $fp))
    if ($img.Width -ne $img.Height) { $iconBad += ("{0} {1}x{2} not square" -f $k, $img.Width, $img.Height) }
    $img.Dispose()
  } catch { $iconBad += "$k unreadable" }
}
if ($iconBad.Count -eq 0) { Line 'OK' 'product icons: 7 square PNGs present' }
else { Line 'FAIL' ('product icons bad: ' + ($iconBad -join '; ')) }

# J 产品落地页路由存在性（PRODUCT_LANDING 路径对应 app 路由，缺页则导航 404）
$landingRoutes = @('voice','face','interpreting','growth','brand')
$missLand = @()
foreach ($r in $landingRoutes) {
  $zh = Join-Path $root ("website/app/$r/page.tsx")
  $en = Join-Path $root ("website/app/en/$r/page.tsx")
  if (-not (Test-Path $zh)) { $missLand += "/$r" }
  if (-not (Test-Path $en)) { $missLand += "/en/$r" }
}
if ($missLand.Count -eq 0) { Line 'OK' 'product landing routes present (zh+en): voice/face/interpreting/growth/brand' }
else { Line 'FAIL' ('missing landing routes: ' + ($missLand -join ', ')) }

# K brand-assets sync 目标：boundless/website 必须在 sync_brand_targets.py 的 SITES 里
$syncPy = Join-Path $root 'brand-assets/sync_brand_targets.py'
if (Test-Path $syncPy) {
  $syncTxt = Get-Content -LiteralPath $syncPy -Raw
  if ($syncTxt -match 'boundless.*website|boundless\\\\website|boundless/website') {
    Line 'OK' 'sync_brand_targets.py targets boundless/website'
  } else {
    Line 'WARN' 'sync_brand_targets.py may not list boundless/website as a sync site'
  }
}

Write-Output '----------------------------------------------------------'
Write-Output ("SUMMARY: FAIL=$fail  WARN=$warn   tracked=" + ((git ls-files | Measure-Object).Count))
if ($fail -gt 0) { Write-Output 'DOCTOR: RED (fix FAILs before release)'; exit 1 }
elseif ($warn -gt 0) { Write-Output 'DOCTOR: AMBER (works; warnings are the completion backlog)' }
else { Write-Output 'DOCTOR: GREEN' }
Write-Output 'REPO_DOCTOR_DONE'
