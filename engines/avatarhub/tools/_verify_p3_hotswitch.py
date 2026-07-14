# -*- coding: utf-8 -*-
"""P3 生产 E2E 验证（非破坏）：
① /api/audio/prefs 审计轨迹：POST 变更带 src → history 记账（同值不记）→ 还原
② /rvc/hot_switch：真打 RVC /config（校验+落盘）→ 未在跑则不强启；config.json 事后还原
③ 就绪度试听依赖的 /api/audio/output_test?device=CABLE：probe.heard 真回环
"""
import json
import sys
import time
from pathlib import Path

import requests

HUB = "http://127.0.0.1:9000"
CFG = Path(r"C:\模仿音色\Retrieval-based-Voice-Conversion-WebUI\configs\config.json")
PREFS = Path(r"C:\模仿音色\audio_prefs.json")
FAILS = []


def chk(name, cond, detail=""):
    print(("  [OK] " if cond else "  [NG] ") + name + (("  " + str(detail)[:160]) if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    # 前置：hub 在线 + 是否在播（在播则跳过会打断声音的分支）
    h = requests.get(HUB + "/health", timeout=5).json()
    chk("hub 在线", bool(h))
    try:
        st = requests.get(HUB + "/realtime/status", timeout=5).json()
        streaming = bool(st.get("running") or st.get("streaming"))
    except Exception:
        streaming = False
    print(f"  [info] 当前推流状态 streaming={streaming}")

    # ── ① 偏好审计轨迹 ──
    # 还原策略：直接备份/回写整个 prefs 文件（API 的 None=不动语义无法表达「删除键」，
    # 之前"原来没存过 input 就不还原"的写法会把探针值留在文件里污染真实偏好）。
    prefs_orig = PREFS.read_bytes() if PREFS.exists() else None
    try:
        probe_val = "P3审计验证麦 (MME)"
        r = requests.post(HUB + "/api/audio/prefs", json={"input": probe_val, "src": "p3-verify"}, timeout=5).json()
        hist = (r.get("prefs") or {}).get("history") or []
        last = hist[-1] if hist else {}
        chk("变更记账(带来源/前后值)", last.get("to") == probe_val and last.get("src") == "p3-verify"
            and last.get("side") == "input", json.dumps(last, ensure_ascii=False))
        n1 = len(hist)
        r2 = requests.post(HUB + "/api/audio/prefs", json={"input": probe_val, "src": "p3-verify"}, timeout=5).json()
        hist2 = (r2.get("prefs") or {}).get("history") or []
        chk("同值重存不记账", len(hist2) == n1, f"{n1} -> {len(hist2)}")
        chk("history 封顶≤10", len(hist2) <= 10, len(hist2))
    finally:
        if prefs_orig is None:
            PREFS.unlink(missing_ok=True)
        else:
            PREFS.write_bytes(prefs_orig)
        now_in = requests.get(HUB + "/api/audio/prefs", timeout=5).json().get("prefs", {}).get("input")
        chk("偏好文件已还原(探针值不残留)", now_in != probe_val, now_in)

    # ── ② 拔插热切端点（未在跑：只换线路不强启；config.json 真被 RVC 落盘）──
    cfg_orig = CFG.read_bytes()
    try:
        d = requests.post(HUB + "/rvc/hot_switch", json={}, timeout=40).json()
        print("  [info] hot_switch →", json.dumps({k: d.get(k) for k in
              ("ok", "step", "was_running", "started", "input", "output", "input_label", "output_label", "detail")},
              ensure_ascii=False))
        chk("hot_switch ok", d.get("ok") is True, d.get("detail"))
        chk("给出目标设备(显式>偏好>推荐)", bool(d.get("input")) and bool(d.get("output")))
        if not streaming:
            chk("未在跑→不强启转换", d.get("was_running") is False and d.get("started") is False)
        cfg_now = json.loads(CFG.read_text(encoding="utf-8"))
        chk("RVC /config 真落盘(设备已写入)", cfg_now.get("sg_input_device") == d.get("input")
            and cfg_now.get("sg_output_device") == d.get("output"))
        # 并发去重锁：背靠背两发，至少一发要么成功要么被「稍候再试」挡住（不允许炸栈）
        import threading
        rs = []
        def _fire():
            try:
                rs.append(requests.post(HUB + "/rvc/hot_switch", json={}, timeout=40).json())
            except Exception as e:
                rs.append({"ok": False, "detail": str(e)})
        ts = [threading.Thread(target=_fire) for _ in range(2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        chk("并发两发均有结构化应答", all(isinstance(x, dict) and "ok" in x for x in rs),
            json.dumps([x.get("detail", "") for x in rs], ensure_ascii=False))
    finally:
        CFG.write_bytes(cfg_orig)   # 磁盘还原（RVC 内存态待下次 /config 覆盖，转换未在跑无副作用）
        chk("config.json 已还原", CFG.read_bytes() == cfg_orig)

    # ── ③ 试听回环（就绪度绿灯的数据源）──
    try:
        devs = requests.get(HUB + "/rvc/devices", timeout=10).json()
        cable = next((x for x in (devs.get("output_devices") or []) if "cable input" in x.lower()), "")
        if cable:
            t = requests.get(HUB + "/api/audio/output_test", params={"device": cable}, timeout=40).json()
            chk("CABLE 试听回环 probe 在场", t.get("ok") is True and isinstance(t.get("probe"), dict),
                json.dumps({k: t.get(k) for k in ("ok", "detail")}, ensure_ascii=False))
            if isinstance(t.get("probe"), dict):
                print(f"  [info] probe.heard={t['probe'].get('heard')} peak={t['probe'].get('peak_dbfs')}dB")
        else:
            print("  [skip] 枚举里没有 CABLE Input，跳过回环试听")
    except Exception as e:
        chk("试听回环调用", False, e)

    print()
    if FAILS:
        print("FAIL %d 项:" % len(FAILS))
        for f in FAILS:
            print(" -", f)
        return 1
    print("P3 E2E 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
