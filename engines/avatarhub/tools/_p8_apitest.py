# -*- coding: utf-8 -*-
"""P8 设备方案中心 API 回归测试：profiles / 切换 / 耦合探针 / 冲突巡检。
用法: python tools/_p8_apitest.py [--probe]
"""
import json
import sys
import io
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:7900"


def api(method: str, path: str, body: dict = None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    fails = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    print("== 1. GET /audio_profile ==")
    r = api("GET", "/audio_profile")
    check("ok", r.get("ok"))
    check("三预设齐全", all(k in r.get("profiles", {}) for k in ("pc", "phone", "headset")),
          ",".join(r.get("profiles", {}).keys()))
    legs = (r.get("resolved") or {}).get("legs", {})
    for leg in ("mic", "listen", "dub_out", "cam"):
        li = legs.get(leg, {})
        check(f"leg:{leg}", "ok" in li, f"{li.get('name','')} {li.get('note','')}")
    print(f"  active={r.get('active')} half_duplex_now={r.get('half_duplex_now')}")

    print("== 2. 切换方案 phone → 回 pc ==")
    r2 = api("POST", "/audio_profile", {"active": "phone"})
    check("切到 phone", r2.get("active") == "phone",
          f"mic_leg={r2['resolved']['legs']['mic'].get('name')} ok={r2['resolved']['legs']['mic'].get('ok')}")
    r3 = api("POST", "/audio_profile", {"active": "pc"})
    check("切回 pc", r3.get("active") == "pc")

    print("== 3. patch 自定义字段 ==")
    r4 = api("POST", "/audio_profile", {"name": "pc", "patch": {"half_duplex": "auto"}})
    check("patch half_duplex=auto", r4.get("profiles", {}).get("pc", {}).get("half_duplex") == "auto")

    print("== 4. GET /conflicts ==")
    r5 = api("GET", "/conflicts")
    check("ok", r5.get("ok"))
    print(f"  issues={json.dumps(r5.get('issues'), ensure_ascii=False)}")
    print(f"  half_duplex={r5.get('half_duplex')}")

    if "--probe" in sys.argv:
        print("== 5. POST /coupling_probe (播 2 声测试音,3 秒) ==")
        r6 = api("POST", "/coupling_probe", {}, timeout=40)
        check("探针执行", r6.get("ok"), r6.get("detail", ""))
        if r6.get("ok"):
            print(f"  coupled={r6['coupled']} margin={r6['margin_db']} path={r6['path']}")

    print()
    if fails:
        print(f"FAILED: {len(fails)} 项 → {fails}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
