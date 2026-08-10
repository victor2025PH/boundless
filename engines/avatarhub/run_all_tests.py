# -*- coding: utf-8 -*-
"""统一测试汇总：运行各 Phase 测试并汇总 PASS/FAIL/SKIP。

兼容两种结果输出格式：
  - 新式: "结果: PASS=.. FAIL=.. SKIP=.."（test_phase5/6/7/8/9/11）
  - 旧式: "...通过: ..."（历史 arch 测试，若存在）
离线即可跑核心套件；带 HUB_URL 时在线用例一并验证。
"""
import os, re, sys, subprocess
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 2026 升级套件（Phase 5–11，统一 PASS/FAIL/SKIP 结果格式）。
# 仅汇总本路线图相关测试；历史 test_phase1/2 等格式/依赖不同，不在此聚合。
SUITE = [
    "test_phase5.py", "test_phase6.py", "test_phase7.py",
    "test_phase8.py", "test_phase9.py", "test_phase11.py", "test_phase12.py",
    # P13 无声事故防线(2026-07-15)：声纹影子模式/告警归因/垫播素材——离线纯逻辑,exit code 判定
    "tools/_p13_vl_shadow_test.py", "tools/_p13_attr_test.py", "tools/_p13_bed_test.py",
    # 设计令牌单源门禁(2026-07-16)：design-tokens.json ↔ brand.css ↔ launcher_theme 三方一致
    "tools/_tokens_lint.py",
    # 窗口身份门禁(2026-07-16)：favicon 双格式 + theme-color + /favicon.ico 路由 + 窗口默认最大化
    # （根治"应用窗口顶着 Edge 图标/浅色标题栏"实锤事故，公约见 设计规范_图标与令牌.md·三）
    "tools/_favicon_lint.py",
    # 控制台拼装一致性(2026-07-16 P2-1)：static/ui.html 是生成产物（源=static/ui_src/），
    # 手改产物或改源忘重跑 _build_ui.py → 红灯
    "tools/_build_ui.py",
    # 冻结态资源定位仿真(2026-07-16)：安装版(PyInstaller)下令牌/图标库必须从 exe 旁 static/ 读到
    # （缺 PySide6 时自动换 .venv_launcher 解释器重跑,两者皆无则 SKIP）
    "tools/_frozen_assets_test.py",
]
files = [f for f in SUITE if os.path.exists(f)]

# [P2-0·2026-07-16] p13 两套件依赖 numpy：用 .venv_launcher 等轻解释器跑会环境性假红。
# 当前解释器缺 numpy 时自动换 facefusion 环境解释器（app_config 单一真相），两者皆无才如实报红。
_NEEDS_NUMPY = {"tools/_p13_vl_shadow_test.py", "tools/_p13_bed_test.py"}
_FF_PY = None


def _runner_for(f: str) -> str:
    global _FF_PY
    if f not in _NEEDS_NUMPY:
        return sys.executable
    try:
        import numpy  # noqa: F401  当前解释器自带 numpy 就不必换
        return sys.executable
    except Exception:
        pass
    if _FF_PY is None:
        try:
            import app_config
            p = app_config.conda_python("facefusion")
            _FF_PY = p if os.path.exists(p) else ""
        except Exception:
            _FF_PY = ""
    return _FF_PY or sys.executable


_re_new = re.compile(r"PASS=(\d+)\s+FAIL=(\d+)\s+SKIP=(\d+)")

# 个别套件需要附加参数（_build_ui 默认动作是"拼装写盘"，套件里只做一致性校验）
_EXTRA_ARGS = {"tools/_build_ui.py": ["--check"]}

tot_pass = tot_fail = tot_skip = 0
ng_files = []
print("=" * 60)
print(" AvatarHub 测试汇总  (HUB_URL=%s)" % os.environ.get("HUB_URL", "<未设置=部分在线用例跳过>"))
print("=" * 60)
for f in files:
    r = subprocess.run([_runner_for(f), f] + _EXTRA_ARGS.get(f, []), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    m = None
    for line in r.stdout.splitlines():
        mm = _re_new.search(line)
        if mm:
            m = mm
    if m:
        p, fa, sk = int(m.group(1)), int(m.group(2)), int(m.group(3))
        tot_pass += p; tot_fail += fa; tot_skip += sk
        mark = "OK" if (r.returncode == 0 and fa == 0) else "NG"
        if mark == "NG":
            ng_files.append(f)
        print(f"  [{mark}] {f:<20} PASS={p:<3} FAIL={fa:<3} SKIP={sk}")
    else:
        mark = "OK" if r.returncode == 0 else "NG"
        if mark == "NG":
            ng_files.append(f)
        print(f"  [{mark}] {f:<20} (EXIT={r.returncode}, 无标准结果行)")

print("-" * 60)
print(f"  合计: PASS={tot_pass}  FAIL={tot_fail}  SKIP={tot_skip}  ·  套件 {len(files)} 个")
if ng_files:
    print(f"  失败套件: {', '.join(ng_files)}")
    sys.exit(1)
print("  ALL GREEN")
sys.exit(0)
