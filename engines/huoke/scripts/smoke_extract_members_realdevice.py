#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FB 群成员打招呼 单独 smoke (2026-04-24 Phase 9).

与 smoke_fb_name_hunter_realdevice 区别: 只测"进群 + 提取 Members",
不发好友请求 / 不打招呼. 10 分钟内能定位 UI 断点.

用法::

    python scripts/smoke_extract_members_realdevice.py \\
        --device 8DWOF6CYY5R8YHX8 --group "ママ友会"

    # 只拿已在某群页面的 dump (不 enter_group):
    python scripts/smoke_extract_members_realdevice.py --device X --no-enter-group
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main():
    ap = argparse.ArgumentParser(description="FB 群成员打招呼 smoke")
    ap.add_argument("--device", required=True)
    ap.add_argument("--group", default="",
                     help="群名(精确匹配); 空=假设已在群页面不 enter_group")
    ap.add_argument("--no-enter-group", action="store_true",
                     help="跳过 enter_group, 只对当前页面执行 extract")
    ap.add_argument("--max-members", type=int, default=15)
    ap.add_argument("--persona", default="jp_female_midlife")
    args = ap.parse_args()

    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation()

    group_name = "" if args.no_enter_group else args.group

    print(f"[start] device={args.device} group={group_name!r} "
          f"persona={args.persona} max={args.max_members}")

    members = fb.extract_group_members(
        group_name=group_name,
        max_members=args.max_members,
        use_llm_scoring=False,
        device_id=args.device,
        persona_key=args.persona,
        phase="growth",   # bypass cold_start 限制
    )

    print(f"\n[result] 提取到 {len(members)} 个成员:")
    for i, m in enumerate(members[:20], 1):
        name = m.get("name") if isinstance(m, dict) else str(m)
        print(f"  {i:2d}. {name!r}")

    # 判定
    if len(members) == 0:
        print("\n[FAIL] 0 成员. 运行 scripts/debug_extract_members_trace.py 诊断 UI")
        sys.exit(1)
    elif len(members) < 3:
        print(f"\n[WARN] 只拿到 {len(members)} 成员 (< 3). 检查是否 scroll 够或"
              f" UI 变化导致漏抓")
        sys.exit(2)
    else:
        print(f"\n[PASS] 提取 ≥ 3 成员 ✓")

    # 查库确认有入 leads pool
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
        leads = store.list_leads(limit=5)
        print(f"\n[leads] store 里最新 5 条 leads:")
        for l in leads[:5]:
            print(f"  id={l.get('lead_id')} name={l.get('name')!r} "
                  f"tags={l.get('tags', [])}")
    except Exception as e:
        print(f"  leads 查询异常: {e}")


if __name__ == "__main__":
    main()
