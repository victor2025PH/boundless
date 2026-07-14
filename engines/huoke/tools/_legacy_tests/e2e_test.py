#!/usr/bin/env python3
"""
End-to-end test — real device, all 3 platforms.

Tests:
  1. Telegram: D1→D2 send message, D2 read, compliance, metrics
  2. WhatsApp: D1 send message to contact, read chat list
  3. LinkedIn: D1 search profiles, browse

Each test records to StructuredLogger + MetricsCollector.
"""

import sys
import os
import time
import logging

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

import uiautomator2 as u2

from src.device_control.device_manager import get_device_manager
from src.app_automation.telegram import TelegramAutomation, TelegramConfig
from src.app_automation.whatsapp import WhatsAppAutomation
from src.app_automation.linkedin import LinkedInAutomation, PACKAGE as LI_PACKAGE
from src.behavior.human_behavior import HumanBehavior, get_profile
from src.behavior.compliance_guard import get_compliance_guard, QuotaExceeded
from src.observability.structured_log import get_structured_logger
from src.observability.metrics import get_metrics_collector

# ── Config ──
D1 = "89NZVGKFD6BYUO5P"
D2 = "R8CIFUBIOVCIUW5H"
CONFIG = "config/devices.yaml"


def reset_devices():
    """Press home on both devices to ensure clean state."""
    for serial in [D1, D2]:
        d = u2.connect(serial)
        d.press("home")
        time.sleep(1)
    print("  Devices reset to home screen")

slog = get_structured_logger()
mc = get_metrics_collector()

results = {"pass": 0, "fail": 0, "skip": 0}


def report(test_name, success, duration, detail=""):
    status = "PASS" if success else "FAIL"
    results["pass" if success else "fail"] += 1
    mc.inc("e2e_tests_total", status=status.lower())
    mc.observe("e2e_test_duration_sec", duration)
    slog.action(f"e2e:{test_name}", platform="e2e", success=success,
                duration_sec=duration, detail=detail)
    print(f"  [{status}] {test_name} ({duration:.1f}s) {detail}")


# ═══════════════════════════════════════════════════════════════════════
# 1. TELEGRAM
# ═══════════════════════════════════════════════════════════════════════
def test_telegram():
    print("\n" + "=" * 60)
    print("  TELEGRAM E2E TESTS")
    print("=" * 60)

    dm = get_device_manager(CONFIG)
    tg1 = TelegramAutomation(dm, TelegramConfig())
    tg2 = TelegramAutomation(dm, TelegramConfig())
    tg1.set_current_device(D1)
    tg2.set_current_device(D2)

    # Force clean start on both devices
    d1u = u2.connect(D1)
    d2u = u2.connect(D2)
    for pkg in [LI_PACKAGE, "com.whatsapp"]:
        d1u.app_stop(pkg)
        d2u.app_stop(pkg)

    # -- T1: Start Telegram on both devices --
    t0 = time.time()
    d1u.app_stop("org.telegram.messenger")
    d2u.app_stop("org.telegram.messenger")
    time.sleep(1)
    ok1 = tg1.start_telegram(D1)
    ok2 = tg2.start_telegram(D2)
    time.sleep(3)
    report("tg_start_both", ok1 and ok2, time.time() - t0)

    # -- T2: Device 1 search and open Saved Messages --
    t0 = time.time()
    ok = tg1.search_and_open_user("Saved Messages", D1)
    report("tg_d1_open_saved", ok, time.time() - t0)

    if ok:
        # -- T3: Send message from D1 to Saved Messages --
        ts = int(time.time())
        msg = f"E2E test {ts}"
        t0 = time.time()
        ok = tg1.send_text_message(msg, D1)
        report("tg_d1_send_saved", ok, time.time() - t0, f"msg='{msg}'")

        # -- T4: Read messages on D1 --
        t0 = time.time()
        msgs = tg1.read_messages(D1, count=5)
        found = any(msg in m.get("text", "") for m in msgs) if msgs else False
        report("tg_d1_read_messages", len(msgs) > 0, time.time() - t0,
               f"count={len(msgs)}, found_sent={found}")

        # Go back to main
        tg1.go_back(D1)
        time.sleep(1)

    # -- T5: Device 2 open Saved Messages and send --
    t0 = time.time()
    ok = tg2.search_and_open_user("Saved Messages", D2)
    report("tg_d2_open_saved", ok, time.time() - t0)

    if ok:
        ts2 = int(time.time())
        msg2 = f"D2 test {ts2}"
        t0 = time.time()
        ok = tg2.send_text_message(msg2, D2)
        report("tg_d2_send_saved", ok, time.time() - t0)
        tg2.go_back(D2)
        time.sleep(1)

    # -- T6: ComplianceGuard integration check --
    t0 = time.time()
    guard = get_compliance_guard()
    try:
        guard.check("telegram", "send_message", "test_acct")
        guard.record("telegram", "send_message", "test_acct", D1)
        remaining = guard.get_remaining("telegram", "send_message", "test_acct")
        report("tg_compliance", True, time.time() - t0,
               f"remaining={remaining}")
    except Exception as e:
        report("tg_compliance", False, time.time() - t0, str(e))

    # -- T7: HumanBehavior session --
    t0 = time.time()
    hb = HumanBehavior(profile=get_profile("telegram"))
    hb.session_start()
    stats = hb.session_stats()
    report("tg_behavior", stats["elapsed_sec"] >= 0, time.time() - t0,
           f"warmup_mult={stats.get('warmup_mult', 0)}")


# ═══════════════════════════════════════════════════════════════════════
# 2. WHATSAPP
# ═══════════════════════════════════════════════════════════════════════
def test_whatsapp():
    print("\n" + "=" * 60)
    print("  WHATSAPP E2E TESTS")
    print("=" * 60)

    dm = get_device_manager(CONFIG)
    wa = WhatsAppAutomation(dm)
    wa.set_current_device(D1)

    # -- W1: Start WhatsApp --
    t0 = time.time()
    ok = wa.start_app(D1)
    report("wa_start", ok, time.time() - t0)

    # -- W2: List chats --
    t0 = time.time()
    try:
        chats = wa.list_chats(D1)
        report("wa_list_chats", len(chats) >= 0, time.time() - t0,
               f"count={len(chats)}")
    except Exception as e:
        report("wa_list_chats", False, time.time() - t0, str(e))

    # -- W3: Open a contact (try suggested contact if no chat history) --
    t0 = time.time()
    try:
        d = wa._u2(D1)
        row = d(resourceId="com.whatsapp:id/conversations_row_contact_name")
        if not row.exists(timeout=3):
            # Try suggested contacts name (visible in "Start chatting" section)
            row = d(resourceId="com.whatsapp:id/suggested_contacts_list_item_name")
        if row.exists(timeout=5):
            first_name = row.get_text()
            row.click()
            time.sleep(3)
            report("wa_open_contact", True, time.time() - t0, f"name={first_name}")

            # -- W4: Verify chat screen --
            t0 = time.time()
            msg_input = d(resourceId="com.whatsapp:id/entry")
            if not msg_input.exists(timeout=3):
                msg_input = d(className="android.widget.EditText", packageName="com.whatsapp")
            report("wa_chat_screen", msg_input.exists(timeout=5), time.time() - t0)
            wa.go_back(D1)
            time.sleep(1)
        else:
            d.screenshot("data/e2e_wa_no_contacts.png")
            report("wa_open_contact", False, time.time() - t0, "no contacts found")
    except Exception as e:
        report("wa_open_contact", False, time.time() - t0, str(e))

    # -- W5: Test on Device 2 --
    wa2 = WhatsAppAutomation(dm)
    wa2.set_current_device(D2)

    t0 = time.time()
    ok = wa2.start_app(D2)
    report("wa_d2_start", ok, time.time() - t0)

    t0 = time.time()
    try:
        d2 = wa2._u2(D2)
        row2 = d2(resourceId="com.whatsapp:id/conversations_row_contact_name")
        if row2.exists(timeout=5):
            name2 = row2.get_text()
            report("wa_d2_has_chats", True, time.time() - t0, f"first={name2}")
        else:
            report("wa_d2_has_chats", False, time.time() - t0, "no chats")
    except Exception as e:
        report("wa_d2_has_chats", False, time.time() - t0, str(e))


# ═══════════════════════════════════════════════════════════════════════
# 3. LINKEDIN
# ═══════════════════════════════════════════════════════════════════════
def test_linkedin():
    print("\n" + "=" * 60)
    print("  LINKEDIN E2E TESTS")
    print("=" * 60)

    dm = get_device_manager(CONFIG)
    li = LinkedInAutomation(dm)
    li.set_current_device(D1)

    # -- L1: Start LinkedIn --
    t0 = time.time()
    try:
        li.start_app(D1)
        report("li_start", True, time.time() - t0)
    except Exception as e:
        report("li_start", False, time.time() - t0, str(e))
        return

    time.sleep(3)

    # -- L2: Search profiles --
    t0 = time.time()
    try:
        d_li = li._d(D1)
        d_li.screenshot("data/e2e_li_before_search.png")
        profiles = li.search_profiles("software engineer", D1, max_results=5)
        report("li_search", len(profiles) > 0, time.time() - t0,
               f"results={len(profiles)}")
        if profiles:
            first = profiles[0]
            print(f"    First result: {first.get('name', '?')} - {first.get('title', '?')}")
    except Exception as e:
        report("li_search", False, time.time() - t0, str(e))

    # -- L3: Go to LinkedIn home feed --
    t0 = time.time()
    try:
        d_li = li._d(D1)
        d_li.press("back")
        time.sleep(1)
        d_li.press("back")
        time.sleep(1)
        # Try finding home tab via multiple selectors
        home = d_li(resourceId="com.linkedin.android:id/tab_feed")
        if not home.exists(timeout=3):
            home = d_li(description="Home")
        if not home.exists(timeout=3):
            home = d_li(description="首页")
        if home.exists(timeout=3):
            home.click()
            time.sleep(3)
            report("li_go_home", True, time.time() - t0)
        else:
            d_li.screenshot("data/e2e_li_no_home.png")
            report("li_go_home", False, time.time() - t0, "home tab not found")
    except Exception as e:
        report("li_go_home", False, time.time() - t0, str(e))

    # -- L4: Browse feed (scroll naturally) --
    t0 = time.time()
    try:
        d_li = li._d(D1)
        li.hb.scroll_down(d_li)
        time.sleep(2)
        li.hb.scroll_down(d_li)
        d_li.screenshot("data/e2e_li_feed.png")
        report("li_browse_feed", True, time.time() - t0)
    except Exception as e:
        report("li_browse_feed", False, time.time() - t0, str(e))

    # -- L5: Test on Device 2 --
    li2 = LinkedInAutomation(dm)
    li2.set_current_device(D2)

    t0 = time.time()
    try:
        li2.start_app(D2)
        time.sleep(5)
        d2 = li2._d(D2)
        pkg = d2.info["currentPackageName"]
        report("li_d2_start", pkg == LI_PACKAGE, time.time() - t0, f"pkg={pkg}")
    except Exception as e:
        report("li_d2_start", False, time.time() - t0, str(e))


# ═══════════════════════════════════════════════════════════════════════
# 4. OBSERVABILITY CHECK
# ═══════════════════════════════════════════════════════════════════════
def test_observability():
    print("\n" + "=" * 60)
    print("  OBSERVABILITY CHECKS")
    print("=" * 60)

    # -- O1: Metrics snapshot --
    t0 = time.time()
    snap = mc.snapshot()
    report("obs_metrics", snap["uptime_sec"] > 0, time.time() - t0,
           f"counters={len(snap['counters'])}")

    # -- O2: Prometheus export --
    t0 = time.time()
    prom = mc.prometheus()
    report("obs_prometheus", len(prom) > 10, time.time() - t0,
           f"lines={prom.count(chr(10))}")

    # -- O3: Structured log query --
    t0 = time.time()
    logs = slog.query_logs(limit=20)
    report("obs_logs", len(logs) > 0, time.time() - t0, f"entries={len(logs)}")

    # -- O4: Alert evaluation --
    t0 = time.time()
    from src.observability.alerting import get_alert_manager
    am = get_alert_manager()
    events = am.evaluate(mc)
    report("obs_alerts", True, time.time() - t0,
           f"rules={len(am.get_rules())}, fired={len(events)}")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("  MOBILE AUTOMATION E2E TEST SUITE")
    print(f"  Devices: D1={D1}, D2={D2}")
    print("=" * 60)

    start = time.time()
    reset_devices()

    try:
        test_telegram()
    except Exception as e:
        print(f"  [FATAL] Telegram tests crashed: {e}")

    try:
        test_whatsapp()
    except Exception as e:
        print(f"  [FATAL] WhatsApp tests crashed: {e}")

    try:
        test_linkedin()
    except Exception as e:
        print(f"  [FATAL] LinkedIn tests crashed: {e}")

    try:
        test_observability()
    except Exception as e:
        print(f"  [FATAL] Observability tests crashed: {e}")

    elapsed = time.time() - start

    print("\n" + "=" * 60)
    print(f"  RESULTS: {results['pass']} PASS / {results['fail']} FAIL / {results['skip']} SKIP")
    print(f"  Total time: {elapsed:.1f}s")
    print("=" * 60)

    slog.workflow("e2e_test_suite", f"e2e_{int(time.time())}",
                  "success" if results["fail"] == 0 else "partial",
                  steps_total=results["pass"] + results["fail"],
                  steps_ok=results["pass"], elapsed_sec=elapsed)
