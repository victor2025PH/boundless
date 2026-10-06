# Fleet 本地门禁：跑完和 CI（仓库根 .github/workflows/chengjie-tests.yml）同口径的 fleet 子集再推。
#
# 为什么要这个脚本（0.3.8）：之前本地只跑 tests/test_*fleet*/*phone*.py，CI 上才跑的
# 「全仓门禁」（静默吞异常 ratchet、模板 bare fetch / 内联颜色 / emoji ratchet 等）漏在本地之外，
# 0.3.7 就是推上去以后才被 test_silent_exception_ratchet 拦下来。这里把它们一起跑。
#
# 跑什么：
#   1) pytest：文件名含 fleet / phone / ratchet 的用例 + 内容引用 src.fleet / fleet_control /
#      fleet_agent / deploy/fleet / fleet_console 的用例（自动发现，新加的 fleet 测试不用改脚本）；
#   2) CI lint job 的只检查钩子（check-yaml / check-merge-conflict / debug-statements），只看本分支改动的文件；
#   3) CI secrets job 的 gitleaks（装了才跑；没装黄字提示跳过）。缺省只扫本分支改动 / 新增的文件
#      （从仓库根跑，.gitleaksignore 生效）；-FullSecrets 扫整个工作区（会扫到未跟踪 / 被忽略的本地文件，
#      Windows 下 .gitleaksignore 的指纹路径分隔符也对不上，结果比 CI 多，只作参考）。
# 注意：test_silent_exception_ratchet 按 **index** 判定（他线未提交的改动不算），所以本分支改过的
# src/ 文件要先 `git add` 再跑；脚本发现 src/ 有未暂存改动会提醒。
#
# 用法（在 engines/chengjie 下或任意目录）：
#   scripts\fleet_tests.ps1                    # fleet 子集 + lint + gitleaks
#   scripts\fleet_tests.ps1 -NoLint            # 只跑 pytest
#   scripts\fleet_tests.ps1 -Base origin/main  # lint 的比较基线（缺省 origin/feat/chengjie-player-care-fleet）
#   scripts\fleet_tests.ps1 -List              # 只列出会跑哪些测试文件
#   scripts\fleet_tests.ps1 -Workers 4         # pytest-xdist 并行（串行约 12 分钟，-n 4 约 4 分钟）
#   scripts\fleet_tests.ps1 -- -k remote_ops   # 之后的参数透传给 pytest
# 兼容 Windows PowerShell 5.1 和 pwsh 7：原生命令不重定向 stderr（5.1 下 2>&1 + Stop 会把 stderr 当异常中断）。
param(
    [switch]$NoLint,
    [switch]$List,
    [switch]$FullSecrets,
    [int]$Workers = 0,
    [string]$Base = "origin/feat/chengjie-player-care-fleet",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$PytestArgs
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    foreach ($k in "AITR_WEB_TOKEN", "AITR_WEB_HOST", "AITR_WEB_PORT") {
        Remove-Item "Env:$k" -ErrorAction SilentlyContinue
    }

    $byName = Get-ChildItem tests -Filter "test_*.py" | Where-Object { $_.Name -match "(fleet|phone|ratchet)" } |
        ForEach-Object { "tests/" + $_.Name }
    $byRef = @(git grep -l -E "src\.fleet|fleet_control|fleet_agent|deploy/fleet|fleet_console" -- "tests/test_*.py")
    $files = @($byName + $byRef | Where-Object { $_ } | Sort-Object -Unique)
    if ($List) { $files; return }
    Write-Host "[fleet-tests] $($files.Count) 个测试文件（含 CI 全仓 ratchet 门禁）"

    $unstaged = @(git diff --name-only -- src)
    if ($unstaged.Count -gt 0) {
        Write-Host ("[fleet-tests] 注意：src/ 有未暂存改动（silent-exception ratchet 只看 index，这些不会被数到）：" +
            ($unstaged -join ", ")) -ForegroundColor Yellow
    }

    $pyArgs = @("-m", "pytest") + $files + @("-q", "-p", "no:cacheprovider", "--timeout=90", "--timeout-method=thread")
    if ($Workers -gt 0) { $pyArgs += @("-n", "$Workers") }
    if ($PytestArgs) { $pyArgs += ($PytestArgs | Where-Object { $_ -ne "--" }) }
    & python @pyArgs
    if ($LASTEXITCODE -ne 0) { Write-Host "[fleet-tests] pytest 失败（exit=$LASTEXITCODE）" -ForegroundColor Red; exit $LASTEXITCODE }

    if (-not $NoLint) {
        # 5.1 下给原生命令重定向 stderr 会生成 ErrorRecord，Stop 时直接中断 → 临时放宽
        $ErrorActionPreference = "Continue"
        $mb = (git merge-base HEAD $Base 2>$null)
        $ErrorActionPreference = "Stop"
        if (-not $mb) { Write-Host "[fleet-tests] 找不到基线 $Base，lint 改为检查 HEAD~1 以来的改动" -ForegroundColor Yellow; $mb = "HEAD~1" }
        $changed = @(git diff --name-only --diff-filter=ACMR $mb -- . | ForEach-Object {
                $p = $_ -replace "^engines/chengjie/", ""; if (Test-Path $p) { $p } })
        $changed += @(git ls-files --others --exclude-standard -- src tests scripts deploy domains fleet_agent)
        $changed = @($changed | Where-Object { $_ } | Sort-Object -Unique)
        if (Get-Command pre-commit -ErrorAction SilentlyContinue) {
            if ($changed.Count -gt 0) {
                foreach ($hook in "check-yaml", "check-merge-conflict", "debug-statements") {
                    & pre-commit run $hook --files @changed
                    if ($LASTEXITCODE -ne 0) { Write-Host "[fleet-tests] $hook 失败" -ForegroundColor Red; exit $LASTEXITCODE }
                }
            } else { Write-Host "[fleet-tests] 相对 $Base 没有改动文件，跳过 lint" }
        } else {
            Write-Host "[fleet-tests] 没装 pre-commit（pip install pre-commit），跳过 lint 钩子" -ForegroundColor Yellow
        }
        if (Get-Command gitleaks -ErrorAction SilentlyContinue) {
            $root = (git rev-parse --show-toplevel)
            $targets = @($root)
            if (-not $FullSecrets) { $targets = @($changed | ForEach-Object { (Resolve-Path $_).Path }) }
            Push-Location $root
            try {
                foreach ($tg in $targets) {
                    & gitleaks dir $tg --no-banner --redact --exit-code 1 --log-level warn
                    if ($LASTEXITCODE -ne 0) { Write-Host "[fleet-tests] gitleaks 发现疑似密钥（已打码）：$tg" -ForegroundColor Red; exit $LASTEXITCODE }
                }
            } finally { Pop-Location }
            Write-Host "[fleet-tests] gitleaks：扫了 $($targets.Count) 个目标，无疑似密钥"
        } else {
            Write-Host "[fleet-tests] 没装 gitleaks，跳过密钥扫描（CI secrets job 会扫）" -ForegroundColor Yellow
        }
    }
    Write-Host "[fleet-tests] 全部通过" -ForegroundColor Green
} finally {
    Pop-Location
}
