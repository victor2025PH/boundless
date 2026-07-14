# -*- coding: utf-8 -*-
"""
Sprint 4 P1 离线冒烟:
  1. tt_funnel_store.record_tt_event + get_tt_funnel_metrics(DB 真写真读)
  2. TikTok _emit_event → 自动埋点 funnel
  3. cross-platform-funnel 拉到 TK 真实埋点
  4. 风控 B 策略:FB 风控注入 → 株连取消同设备 TK pending 任务
"""
from __future__ import annotations
import os
import sys
import tempfile
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 走独立 sqlite,避免污染生产 DB
_TMP_DB = Path(tempfile.mkdtemp()) / "s4_p1_test.db"
os.environ["OPENCLAW_DB_PATH"] = str(_TMP_DB)


def _print(title, ok, detail=""):
    icon = "PASS" if ok else "FAIL"
    print(f"[{icon}] {title}{(' - ' + detail) if detail else ''}")
    return ok


def test_tt_funnel_store_round_trip():
    from src.host.database import init_db
    init_db()
    from src.host.tt_funnel_store import record_tt_event, get_tt_funnel_metrics
    import uuid
    preset = f"warmup_test_{uuid.uuid4().hex[:8]}"

    for st, n in [("exposure", 5), ("interest", 3), ("engagement", 2),
                  ("direct_msg", 2), ("guidance", 1), ("conversion", 1)]:
        for i in range(n):
            rid = record_tt_event("DEV1", st,
                                  target_key=f"user_{i}",
                                  preset_key=preset,
                                  meta={"iter": i})
            assert rid > 0

    m = get_tt_funnel_metrics(preset_key=preset)
    ok1 = _print("exposure=5", m["stage_exposure"] == 5)
    ok2 = _print("interest=3", m["stage_interest"] == 3)
    ok3 = _print("engagement=2", m["stage_engagement"] == 2)
    ok4 = _print("rate_exposure_to_interest ≈ 0.6",
                 abs(m["rate_exposure_to_interest"] - 0.6) < 1e-6,
                 f"got={m['rate_exposure_to_interest']}")
    rid = record_tt_event("", "exposure")
    ok5 = _print("reject empty device_id", rid == 0)
    rid = record_tt_event("DEV1", "wrong_stage")
    ok6 = _print("reject invalid stage", rid == 0)
    return all([ok1, ok2, ok3, ok4, ok5, ok6])


def test_tiktok_emit_event_hook():
    from src.host.database import init_db
    init_db()
    from src.host.tt_funnel_store import get_tt_funnel_metrics

    class FakeTT:
        from src.app_automation.tiktok import TikTokAutomation  # noqa

    import uuid
    dev = f"DEV_HOOK_{uuid.uuid4().hex[:8]}"

    from src.app_automation.tiktok import TikTokAutomation
    tt = TikTokAutomation.__new__(TikTokAutomation)

    tt._emit_event("tiktok.user_followed",
                   username="testguy", device_id=dev)
    tt._emit_event("tiktok.dm_sent",
                   username="testguy", device_id=dev)
    tt._emit_event("tiktok.auto_reply_sent",
                   peer_name="testguy", device_id=dev)
    tt._emit_event("tiktok.wa_referral",
                   username="testguy", device_id=dev)

    m = get_tt_funnel_metrics(device_id=dev)
    ok1 = _print("engagement logged via _emit_event",
                 m["stage_engagement"] == 1, f"got={m['stage_engagement']}")
    ok2 = _print("direct_msg logged", m["stage_direct_msg"] == 1,
                 f"got={m['stage_direct_msg']}")
    ok3 = _print("guidance logged", m["stage_guidance"] == 1,
                 f"got={m['stage_guidance']}")
    ok4 = _print("conversion logged", m["stage_conversion"] == 1,
                 f"got={m['stage_conversion']}")
    return all([ok1, ok2, ok3, ok4])


def test_cross_platform_funnel_reads_real():
    """直接调 _tt_funnel_unified,断言读到真实埋点而不是占位。"""
    from src.host.routers.unified_dashboard import _tt_funnel_unified
    # 沿用上面测试写入的数据
    data = _tt_funnel_unified(since_iso=None)
    vals = data["values"]
    ok1 = _print("cross-platform returns real values",
                 any(v > 0 for v in vals) and data["extra"].get("source") == "tiktok_funnel_events",
                 f"values={vals} source={data['extra'].get('source')}")
    return ok1


def test_risk_auto_heal_cross_cancel():
    """模拟 FB 风控 → 校验 cancel_other_platforms=True 会把 TK pending 干掉。"""
    from src.host import task_store as ts
    from src.host.database import init_db
    init_db()

    fb_tid = ts.create_task(task_type="facebook_add_friend",
                            device_id="DEV_RISK",
                            params={"target": "A"})
    tk_tid = ts.create_task(task_type="tiktok_browse_feed",
                            device_id="DEV_RISK",
                            params={})

    from src.host.risk_auto_heal import CrossPlatformRiskHealer, PlatformRiskConfig
    healer = CrossPlatformRiskHealer()
    cfg = PlatformRiskConfig(platform="facebook", enabled=True, strategy="B",
                             cooldown_seconds=600,
                             cancel_other_platforms=True)
    healer._configs["facebook"] = cfg

    result = healer._cancel_pending("DEV_RISK", only_platform=None)

    fb = ts.get_task(fb_tid)
    tk = ts.get_task(tk_tid)
    ok1 = _print("FB pending cancelled", fb.get("status") == "cancelled",
                 f"status={fb.get('status')}")
    ok2 = _print("TK pending ALSO cancelled (cross-platform)",
                 tk.get("status") == "cancelled",
                 f"status={tk.get('status')}")
    ok3 = _print("result dict has both platforms",
                 "facebook" in result and "tiktok" in result,
                 f"result={result}")
    return all([ok1, ok2, ok3])


def main():
    results = []
    print("── Sprint 4 P1 离线冒烟 ────────────────────────────")
    results.append(("tt_funnel_store", test_tt_funnel_store_round_trip()))
    print()
    results.append(("tt_emit_hook", test_tiktok_emit_event_hook()))
    print()
    results.append(("cross_platform_read", test_cross_platform_funnel_reads_real()))
    print()
    results.append(("risk_cross_cancel", test_risk_auto_heal_cross_cancel()))
    print()
    total_ok = all(ok for _, ok in results)
    print("────────────────────────────────────────────────────")
    print(f"Overall: {'PASS' if total_ok else 'FAIL'}")
    for name, ok in results:
        print(f"  {name}: {'OK' if ok else 'FAIL'}")
    return 0 if total_ok else 1


if __name__ == "__main__":
    sys.exit(main())
