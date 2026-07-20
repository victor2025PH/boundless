#!/usr/bin/env bash
# 回归运行器（防陈旧字节码幽灵 flaky）。
#
# 背景：曾出现随机序下 test_*_event_alias 偶发失败——根因是旧 __pycache__/*.pyc
# （模块级常量与当前源码不一致）被加载。本脚本在跑测前清掉 src/tests 的
# __pycache__，并设 PYTHONDONTWRITEBYTECODE 禁止本次写新 .pyc，从源头杜绝污染。
#
# 全量跑（无透传参数）时，pytest 后追加 UI 视觉回归（tools/ui_regress 的
# capture+compare，与基线逐像素比对）：本机 dev 实例不可达则黄字提示跳过、
# 不算失败；比对超阈值则整体退出码非 0。实例地址默认 http://127.0.0.1:18901，
# 可用环境变量 UI_REGRESS_BASE 覆盖。
#
# 用法：
#   scripts/regression.sh                  # 全量（-n auto，带超时兜底）
#   scripts/regression.sh tests/test_x.py  # 透传额外参数给 pytest
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"
export PYTHONDONTWRITEBYTECODE=1
# 隔离运营态 web env：本机若设过 AITR_WEB_TOKEN/HOST/PORT（如手动起后端对齐桌面），
# config_manager 会读它覆盖测试令牌 → 误报一片 401。回归进程里清掉。
unset AITR_WEB_TOKEN AITR_WEB_HOST AITR_WEB_PORT 2>/dev/null || true
find src tests -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
if [ "$#" -gt 0 ]; then
    python -m pytest "$@" -q --timeout=90 --timeout-method=thread
else
    python -m pytest tests/ -n auto -q --timeout=90 --timeout-method=thread
fi

# UI 视觉回归（仅全量跑；定向跑保持快速迭代，不拍图）：capture 用固定
# mock+冻结时钟拍关键界面，compare 与基线逐像素比对，详见
# tools/ui_regress/README.md。dev 实例不在线属常态（CI/未起后端）→
# 黄字提示跳过、不算失败；capture/compare 非零则按视觉回归记失败。
if [ "$#" -eq 0 ]; then
    ui_base="${UI_REGRESS_BASE:-http://127.0.0.1:18901}"
    if curl -fs --max-time 3 -o /dev/null "$ui_base/login"; then
        echo "[ui-regress] dev 实例在线（$ui_base），跑视觉回归"
        ui_rc=0
        python tools/ui_regress/capture.py --base-url "$ui_base" \
            && python tools/ui_regress/compare.py || ui_rc=$?
        if [ "$ui_rc" -ne 0 ]; then
            printf '\033[31m[ui-regress] 视觉回归失败（exit=%s），diff 图见 tools/ui_regress/shots/diff/\033[0m\n' "$ui_rc"
            exit "$ui_rc"
        fi
        # 十期：功能验收冒烟（CmdK/移动端/vi/深链等 18 项，详见 tools/smoke_acceptance.py）
        echo "[acceptance] 跑功能验收冒烟（$ui_base）"
        acc_rc=0
        python tools/smoke_acceptance.py --base "$ui_base" \
            --token "${SMOKE_ACCEPT_TOKEN:-dev-ui-check}" \
            --out tools/smoke_acceptance.json || acc_rc=$?
        if [ "$acc_rc" -ne 0 ]; then
            printf '\033[31m[acceptance] 功能验收失败（exit=%s），明细见 tools/smoke_acceptance.json\033[0m\n' "$acc_rc"
            exit "$acc_rc"
        fi
    else
        printf '\033[33m[ui-regress] dev 实例不可达（%s），跳过视觉回归\033[0m\n' "$ui_base"
    fi
fi
