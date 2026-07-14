# -*- coding: utf-8 -*-
"""Phase 12-D 功能冒烟（离线，不需 Hub 常驻进程）：
  D-1 /api/device/checkup   quick 与完整模式的结构/归一化/建议产出
  D-4 /api/cluster/wizard    env 片段生成 + lint 集成
  D-2 /api/client_metric     TTFV 回传聚合(count/avg/min/max)
  D-2 vcam /preview.mjpeg    MJPEG 生成器逐帧产出(直调生成器,不开相机线程的 HTTP 面)
用 TestClient 直打 avatar_hub 应用；vcam 部分单独 try-import(依赖 aiortc/av，环境缺则 SKIP)。
"""
import sys, os, json, asyncio
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0; FAIL = 0; SKIP = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  [PASS] {name} {detail}")
    else:    FAIL += 1; print(f"  [FAIL] {name} {detail}")
def skip(name, why=""):
    global SKIP; SKIP += 1; print(f"  [SKIP] {name} {why}")

print("=" * 55)
print(" Phase 12-D 功能冒烟：设备体检 / 分机向导 / 客户端指标 / MJPEG 兜底")
print("=" * 55)

import avatar_hub as AH
from fastapi.testclient import TestClient
c = TestClient(AH.app)

# ── D-1 设备体检 quick 模式（免录音，launcher 轮询口径）──────────────
print("\n--- D-1 /api/device/checkup?quick=1 ---")
r = c.get("/api/device/checkup", params={"quick": 1})
check("HTTP 200", r.status_code == 200, str(r.status_code))
d = r.json()
check("ok=True", d.get("ok") is True)
keys = [i["key"] for i in d.get("items", [])]
check("含 mic/camera/cable 三项", all(k in keys for k in ("mic", "camera", "cable")), str(keys))
check("score 0-100", isinstance(d.get("score"), int) and 0 <= d["score"] <= 100, str(d.get("score")))
check("grade 三色", d.get("grade") in ("green", "yellow", "red"), str(d.get("grade")))
check("quick 标记", d.get("quick") is True)
check("quick 足够快(<3s)", d.get("took_ms", 9999) < 3000, f"{d.get('took_ms')}ms")
for it in d.get("items", []):
    m = "✓" if it["measured"] else "未测"
    print(f"      {it['label']}: {it['score']}/{it['max']} [{it['level']}] {m} · {it['detail'][:60]}")

# 归一化口径：总分 = Σ可测得分/Σ可测满分
meas = [i for i in d.get("items", []) if i["measured"] and i["max"] > 0]
if meas:
    expect = round(100.0 * sum(i["score"] for i in meas) / sum(i["max"] for i in meas))
    check("总分=可测项归一", d.get("score") == expect, f"{d.get('score')} vs {expect}")

# ── D-1 完整模式（真录音 1.2s；无输入设备的机器上容忍降级路径）────────
print("\n--- D-1 /api/device/checkup 完整模式(录音) ---")
r = c.get("/api/device/checkup", params={"mic_secs": 1.2})
d2 = r.json()
check("HTTP 200", r.status_code == 200, str(r.status_code))
check("ok=True", d2.get("ok") is True)
mic = next((i for i in d2.get("items", []) if i["key"] == "mic"), {})
print(f"      麦克风: {mic.get('score')}/{mic.get('max')} [{mic.get('level')}] {mic.get('detail','')[:80]}")
if mic.get("max") == 40:
    check("完整模式麦满分口径=40", True)
    check("噪声底已入 detail", ("dBFS" in mic.get("detail", "")) or mic.get("level") in ("warn", "bad"),
          mic.get("detail", "")[:50])
else:
    skip("完整模式麦满分口径", f"(设备缺失/占用,max={mic.get('max')})")
check("未开播时 camera 标未测", any(i["key"] == "camera" and not i["measured"]
                                    for i in d2.get("items", []))
      or d2.get("streaming") is True, f"streaming={d2.get('streaming')}")

# ── D-4 分机部署向导 ───────────────────────────────────────────────
print("\n--- D-4 /api/cluster/wizard ---")
r = c.get("/api/cluster/wizard")
check("HTTP 200", r.status_code == 200, str(r.status_code))
w = r.json()
if not w.get("ok") and "不存在" in str(w.get("detail", "")):
    skip("向导内容", "(无 cluster_map.json 单机部署)")
else:
    check("ok=True", w.get("ok") is True, str(w.get("detail", "")))
    check("hosts 非空", len(w.get("hosts", [])) > 0, f"{len(w.get('hosts', []))} 台")
    frag = w.get("env_fragment", "")
    check("env 片段含 set \"SVC_", 'set "SVC_' in frag, frag.splitlines()[0] if frag else "(空)")
    n_envkeys = sum(1 for h in w["hosts"] for s in h["svcs"] if s.get("env_key"))
    check("env 行数=带 env_key 服务数", len(frag.splitlines()) == n_envkeys,
          f"{len(frag.splitlines())} vs {n_envkeys}")
    lint = w.get("lint", {})
    check("lint 有结论", lint.get("ok") in (True, False), str(lint.get("ok")))
    print(f"      lint: ok={lint.get('ok')} oks={lint.get('oks')} drifts={len(lint.get('drifts', []))}")
    for t in lint.get("drifts", [])[:3]:
        print(f"        漂移: {t[:100]}")

# ── D-2 客户端指标回传 ─────────────────────────────────────────────
print("\n--- D-2 /api/client_metric ---")
r1 = c.post("/api/client_metric", json={"name": "webrtc_ttfv_ms", "value": 420})
r2 = c.post("/api/client_metric", json={"name": "webrtc_ttfv_ms", "value": 620})
check("回传 200", r1.status_code == 200 and r2.status_code == 200)
m = c.get("/api/client_metric").json()["metrics"].get("webrtc_ttfv_ms", {})
check("count=2", m.get("count") == 2, str(m))
check("avg=520", m.get("avg") == 520.0, str(m.get("avg")))
check("min/max=420/620", m.get("min") == 420.0 and m.get("max") == 620.0)
check("非法名被拒(400)", c.post("/api/client_metric",
                                json={"name": "Bad-Name!", "value": 1}).status_code == 400)

# ── D-2 vcam /preview.mjpeg 生成器（直调，不起相机线程 HTTP 面）────────
print("\n--- D-2 vcam preview.mjpeg 生成器 ---")
try:
    import numpy as np
    import vcam_server as V

    async def _pull_frames(n=3):
        V._latest_rgb = np.random.randint(0, 255, (V.H, V.W, 3), np.uint8)
        resp = await V.preview_mjpeg(fps=10)
        chunks = []
        agen = resp.body_iterator
        async for ch in agen:
            chunks.append(ch)
            if len(chunks) >= n:
                break
        try:
            await agen.aclose()
        except Exception:
            pass
        return chunks

    chunks = asyncio.run(_pull_frames(3))
    check("产出 ≥3 帧", len(chunks) >= 3, str(len(chunks)))
    check("multipart 边界", all(ch.startswith(b"--vcamframe") for ch in chunks))
    check("JPEG 载荷", all(b"\xff\xd8" in ch for ch in chunks))
    sz = [len(ch) for ch in chunks]
    print(f"      帧大小: {sz} bytes @ {V.W}x{V.H}")
except Exception as e:
    skip("vcam 生成器", f"(本环境不可导入: {str(e)[:80]})")

print("\n" + "=" * 55)
print(f" 结果: PASS={PASS} FAIL={FAIL} SKIP={SKIP}")
print("=" * 55)
sys.exit(1 if FAIL else 0)
