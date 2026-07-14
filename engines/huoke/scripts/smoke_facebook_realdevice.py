# -*- coding: utf-8 -*-
"""Facebook 真机端到端 smoke 测试 — Sprint 3 P2 验证脚本。

前置:
  1. 002 号机用 USB 连上(adb devices 能看到)
  2. 已安装 com.facebook.katana + com.facebook.orca
  3. 设备已登录 FB 账号(尤其手动跑过一次,过完手机号验证)
  4. config/devices.yaml 中已配 002

跑法:
  cd c:\\mobile-auto-project
  $env:PYTHONPATH="$pwd"
  python scripts/smoke_facebook_realdevice.py --device 8D7DWWUKQGJRNN79 --groups "Italian Expats Berlin" --extract 5

验证项(7 项,全过 = 链路 OK,可以跑 warmup 预设):
  [1] FB app 能开
  [2] 风控检测器不误报
  [3] enter_group 进群成功
  [4] extract_group_members 拿到 ≥1 人
  [5] scorer 评分写库
  [6] check_messenger_inbox 正常返回(unread 可能 0)
  [7] check_friend_requests_inbox 正常返回
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("smoke")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, help="adb 序列号(必填)")
    ap.add_argument("--groups", default="",
                    help="目标群名称(用 | 分隔多个),空=不进群直接走收件箱")
    ap.add_argument("--extract", type=int, default=5,
                    help="extract_group_members 上限,默认 5(不要太大,留意风险)")
    ap.add_argument("--use-llm", action="store_true",
                    help="启用 scorer v2 LLM 精排(需 LLM_API_KEY)")
    ap.add_argument("--target-country", default="IT",
                    help="目标 GEO,scorer 用,默认 IT")
    ap.add_argument("--with-inbox", action="store_true",
                    help="跑 check_messenger_inbox + check_friend_requests")
    args = ap.parse_args()

    print("\n" + "=" * 60)
    print("Facebook 真机端到端 smoke")
    print(f"  device   : {args.device}")
    print(f"  groups   : {args.groups or '(空 — 跳过进群)'}")
    print(f"  extract  : {args.extract}")
    print(f"  use_llm  : {args.use_llm}")
    print(f"  inbox    : {args.with_inbox}")
    print("=" * 60)

    results = {}

    def ok(name, val=True, detail=""):
        results[name] = (val, detail)
        sym = "OK" if val else "FAIL"
        print(f"  [{sym}] {name}", detail)

    # ─── 加载 facebook automation ───
    print("\n[step 0] 初始化 FacebookAutomation")
    try:
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation()
        ok("FacebookAutomation 初始化", True)
    except Exception as e:
        ok("FacebookAutomation 初始化", False, f"{e}")
        print("无法继续。")
        return _summary(results)

    # ─── 1. 启动 FB app ───
    print("\n[step 1] 启动 FB app + 风控检测")
    try:
        d = fb._u2(args.device)
        d.app_stop("com.facebook.katana")
        time.sleep(0.5)
        d.app_start("com.facebook.katana")
        time.sleep(4)
        ok("FB app 启动", True)
    except Exception as e:
        ok("FB app 启动", False, str(e))
        return _summary(results)

    # ─── 2. 风控检测 ───
    try:
        is_risk, msg = fb._detect_risk_dialog(d)
        ok("风控检测器不误报", not is_risk,
           f"风控信息: {msg[:60]}" if is_risk else "无风控")
        if is_risk:
            print("  ⚠️ 当前设备已处于风控状态,smoke 测试停止。请人工恢复后重试。")
            return _summary(results)
    except Exception as e:
        ok("风控检测器", False, str(e))

    # ─── 3-5. 群相关 ───
    groups = [g.strip() for g in (args.groups or "").split("|") if g.strip()]
    if groups:
        target_group = groups[0]
        print(f"\n[step 3] 进入群组: {target_group}")
        try:
            ok("enter_group", fb.enter_group(target_group, device_id=args.device),
               f"目标={target_group}")
        except Exception as e:
            ok("enter_group", False, str(e))

        print(f"\n[step 4] extract_group_members(max={args.extract})")
        try:
            members = fb.extract_group_members(
                group_name=target_group,
                max_members=args.extract,
                use_llm_scoring=args.use_llm,
                target_country=args.target_country,
                device_id=args.device,
            )
            ok("extract_group_members ≥1", len(members) >= 1,
               f"提取 {len(members)} 人")
            if members:
                print("  抽取样本:")
                for m in members[:3]:
                    print(f"    - {m.get('name', '?'):30s} "
                          f"score={m.get('score', 0):3d} "
                          f"tier={m.get('tier', '?')}")
        except Exception as e:
            ok("extract_group_members", False, str(e))
            members = []

        print("\n[step 5] scorer 持久化校验")
        try:
            from src.host.fb_store import list_groups
            grps = list_groups(device_id=args.device, status="joined", limit=10)
            visited = next((g for g in grps if g["group_name"] == target_group), None)
            if visited:
                ok("group 已落库 + visit_count > 0",
                   visited.get("visit_count", 0) >= 1,
                   f"visit_count={visited.get('visit_count')} "
                   f"extracted={visited.get('extracted_member_count')}")
            else:
                ok("group 已落库", False, "未找到 group 记录")
        except Exception as e:
            ok("scorer 持久化", False, str(e))
    else:
        print("\n[step 3-5] 未指定群名 — 跳过群操作")

    # ─── 6-7. 收件箱 ───
    if args.with_inbox:
        print("\n[step 6] check_messenger_inbox(只读不回)")
        try:
            inbox_stats = fb.check_messenger_inbox(
                auto_reply=False,
                max_conversations=10,
                device_id=args.device,
            )
            ok("check_messenger_inbox 正常返回",
               inbox_stats and inbox_stats.get("opened", False),
               f"对话={inbox_stats.get('conversations_listed', 0)} "
               f"未读已处理={inbox_stats.get('unread_processed', 0)}")
        except Exception as e:
            ok("check_messenger_inbox", False, str(e))

        print("\n[step 7] check_friend_requests_inbox(只读)")
        try:
            fr_stats = fb.check_friend_requests_inbox(
                accept_all=False, safe_accept=True,
                max_requests=5,
                device_id=args.device,
            )
            ok("check_friend_requests_inbox 正常返回",
               fr_stats and fr_stats.get("opened", False),
               f"请求={fr_stats.get('requests_seen', 0)}")
        except Exception as e:
            ok("check_friend_requests_inbox", False, str(e))
    else:
        print("\n[step 6-7] 未传 --with-inbox — 跳过收件箱测试")

    return _summary(results)


def _summary(results: dict):
    passed = sum(1 for v, _ in results.values() if v)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"smoke 总结: {passed}/{total} 通过")
    print("=" * 60)
    for name, (ok, detail) in results.items():
        sym = "OK" if ok else "FAIL"
        print(f"  [{sym}] {name}")
        if not ok and detail:
            print(f"        → {detail}")
    print("=" * 60)
    if passed == total:
        print("✅ 链路全通 — 可以跑 warmup 预设了。")
        print("   推荐:curl -X POST http://localhost:8000/facebook/device/<id>/launch \\")
        print("        -d '{\"preset_key\":\"warmup\"}'")
    else:
        print("⚠️ 有失败项,请先按上面错误排查。")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
