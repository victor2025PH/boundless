# -*- coding: utf-8 -*-
"""
C-level tests: TikTok end-to-end on real device.

Usage: python tests/run_c_level.py [test_name]
  test_name: c1_warmup | c2_phase | c7_recovery | c3_test_follow | c4_smart_follow | all

Requires: Device AIUKQ8WSKZBUQK4X connected with TikTok logged in.
"""
import sys
import os
import time
import json
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENCLAW_API_KEY", "")

DEVICE_ID = "AIUKQ8WSKZBUQK4X"
TARGET_COUNTRY = "italy"

from src.host.database import init_db
init_db()


def test_c1_warmup():
    """C1: TikTok warmup session (5 minutes, low risk)."""
    print("=" * 60)
    print("C1: 养号 warmup 测试 (5分钟)")
    print("=" * 60)

    from src.app_automation.tiktok import TikTokAutomation
    from src.host.device_state import get_device_state_store

    ds = get_device_state_store("tiktok")
    ds.init_device(DEVICE_ID)

    before = ds.get_device_summary(DEVICE_ID)
    print(f"  执行前状态:")
    print(f"    phase={before['phase']}, day={before['day']}")
    print(f"    watched={before['total_watched']}, liked={before['total_liked']}")
    print(f"    algo_score={before['algorithm_score']}")

    tt = TikTokAutomation()
    tt.set_current_device(DEVICE_ID)
    print(f"\n  启动 warmup (duration=5min, country={TARGET_COUNTRY})...")
    start = time.time()

    try:
        stats = tt.warmup_session(
            device_id=DEVICE_ID,
            duration_minutes=5,
            target_country=TARGET_COUNTRY,
        )
        elapsed = time.time() - start

        print(f"\n  Warmup 完成 ({elapsed:.0f}秒):")
        print(f"    stats = {json.dumps(stats, indent=2, ensure_ascii=False)}")

        ds.record_warmup(DEVICE_ID, stats)
        after = ds.get_device_summary(DEVICE_ID)
        print(f"\n  执行后状态:")
        print(f"    phase={after['phase']}, day={after['day']}")
        print(f"    watched={after['total_watched']}, liked={after['total_liked']}")
        print(f"    algo_score={after['algorithm_score']}")

        delta_watched = after['total_watched'] - before['total_watched']
        ok = delta_watched > 0
        status = "PASS" if ok else "FAIL"
        print(f"\n  [C1] 结果: {status} | 新观看 {delta_watched} 个视频")
        return {"status": status, "stats": stats, "elapsed": elapsed,
                "new_watched": delta_watched}
    except Exception as e:
        elapsed = time.time() - start
        print(f"\n  [C1] ERROR ({elapsed:.0f}秒): {e}")
        traceback.print_exc()
        return {"status": "ERROR", "error": str(e), "elapsed": elapsed}


def test_c2_phase_check():
    """C2: Verify phase determination and algorithm score tracking."""
    print("=" * 60)
    print("C2: 阶段推进验证")
    print("=" * 60)

    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")

    summary = ds.get_device_summary(DEVICE_ID)
    phase = summary["phase"]
    algo = summary["algorithm_score"]
    watched = summary["total_watched"]
    day = summary["day"]

    print(f"  当前状态:")
    print(f"    phase     = {phase}")
    print(f"    day       = {day}")
    print(f"    watched   = {watched}")
    print(f"    algo_score= {algo}")

    expected_phase = ds.determine_phase(DEVICE_ID)
    print(f"  determine_phase() = {expected_phase}")

    phase_ok = phase == expected_phase
    algo_ok = algo >= 0.0
    status = "PASS" if phase_ok and algo_ok else "FAIL"
    print(f"\n  [C2] 结果: {status} | phase一致={phase_ok} | algo有效={algo_ok}")
    return {"status": status, "phase": phase, "algo_score": algo,
            "day": day, "watched": watched}


def test_c7_recovery():
    """C7: Recovery mode test — force recovery, verify params, exit."""
    print("=" * 60)
    print("C7: 恢复模式测试")
    print("=" * 60)

    from src.behavior.adaptive_compliance import get_adaptive_compliance
    from src.host.device_state import get_device_state_store

    ac = get_adaptive_compliance()
    ds = get_device_state_store("tiktok")
    test_dev = f"{DEVICE_ID}::recovery_test"

    print("  1. 触发恢复模式...")
    ac.force_recovery(test_dev, "c7_test")
    assert ac.is_recovering(test_dev), "Should be recovering"
    print(f"     is_recovering = True ✓")

    print("  2. 验证敏感操作被阻止...")
    for action in ["follow", "send_dm", "comment"]:
        blocked = ac.should_skip(test_dev, action)
        symbol = "✓" if blocked else "✗"
        print(f"     should_skip({action}) = {blocked} {symbol}")
        assert blocked, f"{action} should be blocked"

    print("  3. 验证被动操作允许...")
    for action in ["browse_feed", "like", "search"]:
        allowed = not ac.should_skip(test_dev, action)
        symbol = "✓" if allowed else "✗"
        print(f"     allowed({action}) = {allowed} {symbol}")

    print("  4. 验证恢复 warmup 参数...")
    params = ac.get_recovery_warmup_params(test_dev)
    print(f"     phase={params['phase']}, duration={params['duration_minutes']}min")
    print(f"     like_prob={params['like_probability']}, comment_prob={params['comment_post_prob']}")
    assert params["phase"] == "cold_start"
    assert params["comment_post_prob"] == 0.0

    print("  5. 模拟 3 次恢复 session...")
    for i in range(3):
        ac.record_recovery_session(test_dev)
        still = ac.is_recovering(test_dev)
        print(f"     session {i+1}: is_recovering = {still}")

    exited = not ac.is_recovering(test_dev)
    print(f"  6. 恢复退出: {exited}")

    profile = ac.get_risk_profile(test_dev)
    print(f"  7. 最终 risk_score={profile['risk_score']:.3f}, "
          f"level={profile['risk_level']}")

    status = "PASS" if exited else "FAIL"
    print(f"\n  [C7] 结果: {status}")
    return {"status": status, "exited": exited, "final_profile": profile}


def test_c3_test_follow():
    """C3: Test follow capability (random test)."""
    print("=" * 60)
    print("C3: 关注能力测试")
    print("=" * 60)

    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")
    summary = ds.get_device_summary(DEVICE_ID)

    print(f"  当前状态: phase={summary['phase']}, can_follow={summary['can_follow']}")

    if summary["phase"] == "cold_start":
        print(f"  [C3] SKIP — 设备仍在 cold_start 阶段，需要先完成更多 warmup")
        return {"status": "SKIP", "reason": "cold_start phase"}

    from src.app_automation.tiktok import TikTokAutomation
    tt = TikTokAutomation()
    tt.set_current_device(DEVICE_ID)

    print(f"  尝试测试关注...")
    try:
        result = tt.test_follow(device_id=DEVICE_ID)
        print(f"  测试结果: {json.dumps(result, indent=2, ensure_ascii=False)}")
        status = "PASS"
        print(f"\n  [C3] 结果: {status}")
        return {"status": status, "result": result}
    except Exception as e:
        print(f"\n  [C3] ERROR: {e}")
        traceback.print_exc()
        return {"status": "ERROR", "error": str(e)}


def test_c4_smart_follow():
    """C4: Smart follow with target filtering (max 2 follows)."""
    print("=" * 60)
    print("C4: 智能关注测试 (max 2)")
    print("=" * 60)

    from src.host.device_state import get_device_state_store
    ds = get_device_state_store("tiktok")
    summary = ds.get_device_summary(DEVICE_ID)

    if not summary.get("can_follow"):
        print(f"  [C4] SKIP — 设备尚未解锁关注能力")
        return {"status": "SKIP", "reason": "follow not unlocked"}

    from src.app_automation.tiktok import TikTokAutomation
    from src.app_automation.target_filter import TargetProfile

    tt = TikTokAutomation()
    tt.set_current_device(DEVICE_ID)
    target = TargetProfile(country="italy", gender="male", min_age=30)

    print(f"  目标: {target}")
    print(f"  执行 smart_follow (max_follows=2)...")

    try:
        result = tt.smart_follow(
            device_id=DEVICE_ID,
            target=target,
            max_follows=2,
            target_country=TARGET_COUNTRY,
        )
        print(f"  结果: {json.dumps(result, indent=2, ensure_ascii=False)}")
        status = "PASS"
        print(f"\n  [C4] 结果: {status}")
        return {"status": status, "result": result}
    except Exception as e:
        print(f"\n  [C4] ERROR: {e}")
        traceback.print_exc()
        return {"status": "ERROR", "error": str(e)}


def test_c5_check_inbox():
    """C5: Check TikTok inbox for new messages."""
    print("=" * 60)
    print("C5: 收件箱检查")
    print("=" * 60)

    from src.app_automation.tiktok import TikTokAutomation
    tt = TikTokAutomation()
    tt.set_current_device(DEVICE_ID)

    print(f"  扫描收件箱...")
    try:
        result = tt.check_inbox(device_id=DEVICE_ID, max_conversations=3)
        print(f"  结果: {json.dumps(result, indent=2, ensure_ascii=False)}")
        status = "PASS"
        print(f"\n  [C5] 结果: {status}")
        return {"status": status, "result": result}
    except Exception as e:
        print(f"\n  [C5] ERROR: {e}")
        traceback.print_exc()
        return {"status": "ERROR", "error": str(e)}


if __name__ == "__main__":
    test_name = sys.argv[1] if len(sys.argv) > 1 else "all"

    test_map = {
        "c1_warmup": test_c1_warmup,
        "c2_phase": test_c2_phase_check,
        "c7_recovery": test_c7_recovery,
        "c3_test_follow": test_c3_test_follow,
        "c4_smart_follow": test_c4_smart_follow,
        "c5_inbox": test_c5_check_inbox,
    }

    if test_name == "all":
        order = ["c1_warmup", "c2_phase", "c7_recovery",
                 "c3_test_follow", "c4_smart_follow", "c5_inbox"]
    elif test_name in test_map:
        order = [test_name]
    else:
        print(f"Unknown test: {test_name}")
        print(f"Available: {', '.join(test_map.keys())}, all")
        sys.exit(1)

    all_results = {}
    for name in order:
        try:
            all_results[name] = test_map[name]()
        except Exception as e:
            all_results[name] = {"status": "ERROR", "error": str(e)}
            traceback.print_exc()
        print()

    print("\n" + "=" * 60)
    print("C级测试总结")
    print("=" * 60)
    for name, r in all_results.items():
        print(f"  {name}: {r['status']}")
    passed = sum(1 for r in all_results.values() if r["status"] in ("PASS",))
    skipped = sum(1 for r in all_results.values() if r["status"] == "SKIP")
    failed = sum(1 for r in all_results.values() if r["status"] in ("FAIL", "ERROR"))
    print(f"\n  PASS={passed} SKIP={skipped} FAIL/ERROR={failed}")
