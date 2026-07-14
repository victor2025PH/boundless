# -*- coding: utf-8 -*-
"""
Sprint 4 P2 离线冒烟 (geo cross-source):
  1. _lookup_ip 两源一致 → cross_checked=True, conflict=False
  2. _lookup_ip 两源冲突 → 启用第三源,majority 胜出
  3. _lookup_ip 三源各不同 → conflict=True,保守返回第一个
  4. _lookup_ip 全失败 → None
  5. task_dispatch_gate 在 source_conflict=True & fail_open=false 时拒绝
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _print(title, ok, detail=""):
    icon = "PASS" if ok else "FAIL"
    print(f"[{icon}] {title}{(' - ' + detail) if detail else ''}")
    return ok


def _make_fake_query(mapping):
    def _fake(ip, source, timeout=6):
        cc = mapping.get(source["name"])
        if cc is None:
            return None
        return {
            "country": f"Country-{cc}",
            "country_code": cc,
            "hosting": False,
            "proxy": False,
            "method": source["name"],
        }
    return _fake


def test_all_agree():
    from src.behavior import geo_check
    with mock.patch.object(geo_check, "_query_one_source",
                            _make_fake_query({"ip-api.com": "IT",
                                              "ipapi.co": "IT"})):
        r = geo_check._lookup_ip("1.2.3.4")
    ok1 = _print("two sources agree → returned",
                 r is not None and r["country_code"] == "IT")
    ok2 = _print("cross_checked=True", bool(r and r.get("_cross_checked")))
    ok3 = _print("no conflict", not (r and r.get("_conflict")))
    return ok1 and ok2 and ok3


def test_two_disagree_third_votes():
    from src.behavior import geo_check
    with mock.patch.object(geo_check, "_query_one_source",
                            _make_fake_query({"ip-api.com": "IT",
                                              "ipapi.co": "FR",
                                              "ipwhois.io": "IT"})):
        r = geo_check._lookup_ip("1.2.3.4")
    ok1 = _print("majority (IT) wins",
                 r is not None and r["country_code"] == "IT",
                 f"got={r and r.get('country_code')}")
    ok2 = _print("conflict=True (because 3rd vote disagreed with one)",
                 bool(r and r.get("_conflict")))
    ok3 = _print("cross_checked=True", bool(r and r.get("_cross_checked")))
    sources = (r or {}).get("sources", [])
    ok4 = _print("sources has 3 entries", len(sources) == 3,
                 f"sources={sources}")
    return all([ok1, ok2, ok3, ok4])


def test_three_all_different():
    from src.behavior import geo_check
    with mock.patch.object(geo_check, "_query_one_source",
                            _make_fake_query({"ip-api.com": "IT",
                                              "ipapi.co": "FR",
                                              "ipwhois.io": "DE"})):
        r = geo_check._lookup_ip("1.2.3.4")
    ok1 = _print("3-way disagreement → returns first", r is not None)
    ok2 = _print("conflict=True", bool(r and r.get("_conflict")))
    return ok1 and ok2


def test_all_fail():
    from src.behavior import geo_check
    with mock.patch.object(geo_check, "_query_one_source",
                            _make_fake_query({})):
        r = geo_check._lookup_ip("1.2.3.4")
    return _print("all sources fail → None", r is None)


def test_gate_rejects_on_conflict():
    """模拟 matches=True but conflict=True,fail_open=false → gate 拒绝."""
    from src.host import task_dispatch_gate as gate
    from src.behavior.geo_check import GeoCheckResult

    fake_geo = GeoCheckResult(
        device_id="DEV",
        public_ip="1.2.3.4",
        detected_country="Italy",
        detected_country_code="IT",
        expected_country="italy",
        matches=True,
        cross_checked=True,
        source_conflict=True,
        sources=[
            {"method": "ip-api.com", "country": "Italy", "country_code": "IT"},
            {"method": "ipapi.co", "country": "France", "country_code": "FR"},
            {"method": "ipwhois.io", "country": "Italy", "country_code": "IT"},
        ],
    )

    class FakeDM:
        def execute_adb_command(self, cmd, device_id=None):
            return True, "1.2.3.4" if "ip" in cmd else ""
        def list_devices(self): return []

    fake_policy = {
        "gate_mode": "balanced",
        "manual_gate": {
            "enforce_preflight": True,
            "enforce_geo_for_risky": True,
            "geo_fail_open": False,
            "allow_param_bypass": False,
            "default_expected_country": "italy",
            "risky_task_prefixes": ["facebook_"],
        },
    }

    class FakePf:
        passed = True
        blocked_step = ""
        blocked_reason = ""
        def to_dict(self): return {"passed": True}

    with mock.patch("src.behavior.geo_check.check_device_geo",
                     return_value=fake_geo), \
         mock.patch("src.host.task_policy.load_task_execution_policy",
                     return_value=fake_policy), \
         mock.patch("src.host.preflight.run_preflight",
                     return_value=FakePf()):
        try:
            res = gate.evaluate_task_gate_detailed(
                task={"type": "facebook_add_friend",
                      "params": {"target_country": "italy"}},
                resolved_device_id="DEV",
                config_path="dummy",
            )
            allowed = getattr(res, "allowed", None)
            hint = getattr(res, "hint_code", "")
            ok1 = _print("gate rejects task on source_conflict",
                         allowed is False,
                         f"allowed={allowed} hint={hint} reason={getattr(res,'reason','')[:80]}")
            ok2 = _print("hint_code=geo_cross_source_conflict",
                         hint == "geo_cross_source_conflict",
                         f"hint={hint}")
            return ok1 and ok2
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"[INFO] gate eval failed ({type(e).__name__}: {e})")
            return False


def main():
    results = []
    print("── Sprint 4 P2 离线冒烟 ────────────────────────────")
    results.append(("all_agree", test_all_agree()))
    print()
    results.append(("majority_vote", test_two_disagree_third_votes()))
    print()
    results.append(("three_disagree", test_three_all_different()))
    print()
    results.append(("all_fail", test_all_fail()))
    print()
    results.append(("gate_rejects", test_gate_rejects_on_conflict()))
    print()
    total_ok = all(ok for _, ok in results)
    print("────────────────────────────────────────────────────")
    print(f"Overall: {'PASS' if total_ok else 'FAIL'}")
    for name, ok in results:
        print(f"  {name}: {'OK' if ok else 'FAIL'}")
    return 0 if total_ok else 1


if __name__ == "__main__":
    sys.exit(main())
