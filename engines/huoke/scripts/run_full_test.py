# -*- coding: utf-8 -*-
"""
完整自动化流程测试 — 01号手机。

按顺序执行:
  C1. warmup 养号 (5分钟)
  C2. 阶段检查 + 算法学习分数
  C3. 测试关注能力
  C4. 种子账号关注流程 (如果C3通过)
  C5. 检查收件箱
  C6. 恢复模式验证
  C7. 统计数据汇总
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

os.environ["PYTHONUNBUFFERED"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEVICE_ID = "AIUKQ8WSKZBUQK4X"
TARGET_COUNTRY = "italy"

results = {}


def p(msg: str):
    print(msg, flush=True)


def banner(step: str, title: str):
    p(f"\n{'='*60}")
    p(f"  [{step}] {title}")
    p(f"{'='*60}")


def record(step: str, status: str, details: dict = None):
    results[step] = {"status": status, "details": details or {}}
    icon = "PASS" if status == "PASS" else ("SKIP" if status == "SKIP" else "FAIL")
    p(f"\n  >>> [{step}] {icon} <<<")
    if details:
        for k, v in details.items():
            p(f"      {k}: {v}")


# ═══════════════════════════════════════════════════════════════
# C0: VPN 状态确认
# ═══════════════════════════════════════════════════════════════
banner("C0", "VPN 状态确认")

from src.behavior.vpn_manager import get_vpn_manager
vm = get_vpn_manager()
vpn_status = vm.status(DEVICE_ID)
p(f"  Connected: {vpn_status.connected}")
p(f"  Has tun0: {vpn_status.has_tun}")
p(f"  Has notification: {vpn_status.has_notification}")
p(f"  Config: {vpn_status.config_name}")

if not vpn_status.connected and not vpn_status.has_tun:
    p("  VPN 未连接，尝试重连...")
    vm.ensure_connected(DEVICE_ID)
    vpn_status = vm.status(DEVICE_ID)

record("C0", "PASS" if (vpn_status.has_tun or vpn_status.connected) else "WARN",
       {"tun0": vpn_status.has_tun, "connected": vpn_status.connected})


# ═══════════════════════════════════════════════════════════════
# C1: Warmup 养号
# ═══════════════════════════════════════════════════════════════
banner("C1", "Warmup 养号 (5分钟)")

try:
    from src.app_automation.tiktok import TikTokAutomation
    from src.device_control.device_manager import get_device_manager
    from src.host.device_registry import DEFAULT_DEVICES_YAML

    manager = get_device_manager(DEFAULT_DEVICES_YAML)
    tt = TikTokAutomation(manager)
    tt.set_current_device(DEVICE_ID)

    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")
    ds.init_device(DEVICE_ID)

    phase = ds.determine_phase(DEVICE_ID)
    p(f"  当前阶段: {phase}")

    stats = tt.warmup_session(
        duration_minutes=5,
        target_country=TARGET_COUNTRY,
        phase=phase,
        device_id=DEVICE_ID,
    )
    ds.record_warmup(DEVICE_ID, stats)

    watched = stats.get("watched", 0)
    italian = stats.get("italian_watched", 0)
    liked = stats.get("liked", 0)
    ratio = f"{italian/watched*100:.0f}%" if watched > 0 else "N/A"

    record("C1", "PASS" if watched > 0 else "FAIL", {
        "watched": watched,
        "italian_watched": italian,
        "ratio": ratio,
        "liked": liked,
        "phase": phase,
        "duration_sec": stats.get("duration_sec", 0),
    })
except Exception as e:
    traceback.print_exc()
    record("C1", "FAIL", {"error": str(e)})


# ═══════════════════════════════════════════════════════════════
# C2: 阶段检查 + 算法学习分数
# ═══════════════════════════════════════════════════════════════
banner("C2", "阶段检查 + 算法学习分数")

try:
    ds = get_device_state_store("tiktok")
    phase = ds.determine_phase(DEVICE_ID)
    summary = ds.get_device_summary(DEVICE_ID)
    algo_score = summary.get("algorithm_score", 0)

    record("C2", "PASS", {
        "phase": phase,
        "algorithm_score": f"{algo_score:.1%}" if isinstance(algo_score, float) else algo_score,
        "total_watched": summary.get("total_watched", 0),
        "days_active": summary.get("days_active", 0),
        "can_follow": summary.get("can_follow", False),
    })
except Exception as e:
    traceback.print_exc()
    record("C2", "FAIL", {"error": str(e)})


# ═══════════════════════════════════════════════════════════════
# C3: 测试关注能力
# ═══════════════════════════════════════════════════════════════
banner("C3", "测试关注能力")

try:
    can_follow = tt.test_follow(device_id=DEVICE_ID)
    ds.mark_can_follow(DEVICE_ID, can_follow)
    ds.record_follow_test(DEVICE_ID)
    record("C3", "PASS", {"can_follow": can_follow})
except Exception as e:
    traceback.print_exc()
    record("C3", "FAIL", {"error": str(e)})


# ═══════════════════════════════════════════════════════════════
# C4: 种子账号关注流程
# ═══════════════════════════════════════════════════════════════
banner("C4", "种子账号关注流程")

c3_result = results.get("C3", {})
can_follow = c3_result.get("details", {}).get("can_follow", False)

if can_follow:
    try:
        from src.app_automation.target_filter import TargetProfile
        profile = TargetProfile(
            country=TARGET_COUNTRY,
            gender="male",
            min_age=30,
        )

        follow_stats = tt.smart_follow(
            target=profile,
            max_follows=3,
            device_id=DEVICE_ID,
        )

        record("C4", "PASS", {
            "followed": follow_stats.get("followed", 0),
            "browsed": follow_stats.get("browsed", 0),
            "filtered": follow_stats.get("filtered", 0),
            "seed_account": follow_stats.get("seed_account", ""),
        })
    except Exception as e:
        traceback.print_exc()
        record("C4", "FAIL", {"error": str(e)})
else:
    record("C4", "SKIP", {"reason": "C3 关注能力未通过"})


# ═══════════════════════════════════════════════════════════════
# C5: 检查收件箱
# ═══════════════════════════════════════════════════════════════
banner("C5", "检查收件箱")

try:
    inbox = tt.check_inbox(device_id=DEVICE_ID, max_conversations=5)
    record("C5", "PASS", {
        "conversations_checked": inbox.get("checked", 0),
        "new_messages": inbox.get("new_messages", 0),
        "replied": inbox.get("replied", 0),
    })
except Exception as e:
    traceback.print_exc()
    record("C5", "FAIL", {"error": str(e)})


# ═══════════════════════════════════════════════════════════════
# C6: 恢复模式验证
# ═══════════════════════════════════════════════════════════════
banner("C6", "恢复模式验证")

try:
    from src.behavior.adaptive_compliance import get_adaptive_compliance
    ac = get_adaptive_compliance()

    is_recovering = ac.is_recovering(DEVICE_ID)
    risk_profile = ac.get_risk_profile(DEVICE_ID)

    record("C6", "PASS", {
        "is_recovering": is_recovering,
        "risk_level": risk_profile.get("level", "unknown"),
        "risk_score": f"{risk_profile.get('score', 0):.2f}",
        "actions_recorded": risk_profile.get("total_actions", 0),
    })
except Exception as e:
    traceback.print_exc()
    record("C6", "FAIL", {"error": str(e)})


# ═══════════════════════════════════════════════════════════════
# C7: 统计数据汇总
# ═══════════════════════════════════════════════════════════════
banner("C7", "统计数据汇总")

try:
    summary = ds.get_device_summary(DEVICE_ID)
    record("C7", "PASS", {
        "phase": summary.get("phase", ""),
        "total_watched": summary.get("total_watched", 0),
        "total_followed": summary.get("total_followed", 0),
        "total_liked": summary.get("total_liked", 0),
        "days_active": summary.get("days_active", 0),
        "can_follow": summary.get("can_follow", False),
        "algorithm_score": summary.get("algorithm_score", 0),
    })
except Exception as e:
    traceback.print_exc()
    record("C7", "FAIL", {"error": str(e)})


# ═══════════════════════════════════════════════════════════════
# 最终报告
# ═══════════════════════════════════════════════════════════════
p(f"\n{'='*60}")
p(f"  最终报告 — 设备: {DEVICE_ID}")
p(f"{'='*60}")

passed = sum(1 for r in results.values() if r["status"] == "PASS")
failed = sum(1 for r in results.values() if r["status"] == "FAIL")
skipped = sum(1 for r in results.values() if r["status"] == "SKIP")

for step, r in results.items():
    icon = {"PASS": "[OK]", "FAIL": "[!!]", "SKIP": "[--]", "WARN": "[??]"}.get(
        r["status"], "[??]")
    details_str = " | ".join(f"{k}={v}" for k, v in r.get("details", {}).items())
    p(f"  {icon} {step}: {details_str}")

p(f"\n  总计: {passed} PASS / {failed} FAIL / {skipped} SKIP")
p(f"{'='*60}")
