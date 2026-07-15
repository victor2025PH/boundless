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
]
files = [f for f in SUITE if os.path.exists(f)]

_re_new = re.compile(r"PASS=(\d+)\s+FAIL=(\d+)\s+SKIP=(\d+)")

tot_pass = tot_fail = tot_skip = 0
ng_files = []
print("=" * 60)
print(" AvatarHub 测试汇总  (HUB_URL=%s)" % os.environ.get("HUB_URL", "<未设置=部分在线用例跳过>"))
print("=" * 60)
for f in files:
    r = subprocess.run([sys.executable, f], capture_output=True, text=True,
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
