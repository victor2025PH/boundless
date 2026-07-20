# 回归运行器（防陈旧字节码幽灵 flaky）。
#
# 背景：曾出现随机序下 test_*_event_alias 偶发失败——根因是上个会话遗留的旧
# __pycache__/*.pyc（模块级常量与当前源码不一致）被加载。本脚本在跑测前清掉
# src/tests 的 __pycache__，并设 PYTHONDONTWRITEBYTECODE 禁止本次写新 .pyc，
# 从源头杜绝该类污染。
#
# 全量跑（无透传参数）时，pytest 后追加 UI 视觉回归（tools\ui_regress 的
# capture+compare，与基线逐像素比对）：本机 dev 实例不可达则黄字提示跳过、
# 不算失败；比对超阈值则整体退出码非 0。实例地址默认 http://127.0.0.1:18901，
# 可用环境变量 UI_REGRESS_BASE 覆盖。
#
# 用法：
#   scripts\regression.ps1                 # 全量（-n auto，带超时兜底）
#   scripts\regression.ps1 tests\test_x.py # 透传额外参数给 pytest
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    # 隔离运营态 web env：本机若设过 AITR_WEB_TOKEN/HOST/PORT（如手动起后端对齐桌面），
    # config_manager 会读它覆盖测试令牌 → 误报一片 401。回归子进程里清掉。
    foreach ($k in "AITR_WEB_TOKEN", "AITR_WEB_HOST", "AITR_WEB_PORT") {
        Remove-Item "Env:$k" -ErrorAction SilentlyContinue
    }
    Get-ChildItem -Recurse -Directory -Filter "__pycache__" -Path src, tests `
        -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    if ($args.Count -gt 0) {
        python -m pytest @args -q --timeout=90 --timeout-method=thread
    } else {
        python -m pytest tests/ -n auto -q --timeout=90 --timeout-method=thread
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    # UI 视觉回归（仅全量跑；定向跑保持快速迭代，不拍图）：capture 用固定
    # mock+冻结时钟拍关键界面，compare 与基线逐像素比对，详见
    # tools\ui_regress\README.md。dev 实例不在线属常态（CI/未起后端）→
    # 黄字提示跳过、不算失败；capture/compare 非零则按视觉回归记失败。
    if ($args.Count -eq 0) {
        $uiBase = $env:UI_REGRESS_BASE
        if (-not $uiBase) { $uiBase = "http://127.0.0.1:18901" }
        $devUp = $true
        try {
            $null = Invoke-WebRequest -Uri "$uiBase/login" -Method GET `
                -TimeoutSec 3 -UseBasicParsing
        } catch {
            $devUp = $false
            Write-Host "[ui-regress] dev 实例不可达（$uiBase），跳过视觉回归" `
                -ForegroundColor Yellow
        }
        if ($devUp) {
            Write-Host "[ui-regress] dev 实例在线（$uiBase），跑视觉回归"
            python tools\ui_regress\capture.py --base-url $uiBase
            if ($LASTEXITCODE -eq 0) { python tools\ui_regress\compare.py }
            if ($LASTEXITCODE -ne 0) {
                $msg = "[ui-regress] 视觉回归失败（exit=$LASTEXITCODE），" +
                       "diff 图见 tools\ui_regress\shots\diff\"
                Write-Host $msg -ForegroundColor Red
                exit $LASTEXITCODE
            }
            # 十期：功能验收冒烟（CmdK/移动端/vi/深链等 21 项，详见 tools\smoke_acceptance.py）
            $accToken = $env:SMOKE_ACCEPT_TOKEN
            if (-not $accToken) { $accToken = "dev-ui-check" }
            Write-Host "[acceptance] 跑功能验收冒烟（$uiBase）"
            python tools\smoke_acceptance.py --base $uiBase --token $accToken `
                --out tools\smoke_acceptance.json
            if ($LASTEXITCODE -ne 0) {
                Write-Host "[acceptance] 功能验收失败（exit=$LASTEXITCODE），明细见 tools\smoke_acceptance.json" `
                    -ForegroundColor Red
                exit $LASTEXITCODE
            }
            # 十四期：会话列表性能阈值回归（500 条 mock，抓量级劣化，详见 tools\perf_conv_list.py）
            Write-Host "[perf] 跑会话列表性能回归（$uiBase）"
            python tools\perf_conv_list.py --base $uiBase --token $accToken `
                --out tools\perf_conv_list.json
            if ($LASTEXITCODE -ne 0) {
                Write-Host "[perf] 性能回归超阈值（exit=$LASTEXITCODE），明细见 tools\perf_conv_list.json" `
                    -ForegroundColor Red
                exit $LASTEXITCODE
            }
        }
    }
} finally {
    Pop-Location
}
