# -*- coding: utf-8 -*-
"""P8 E2E：双脸图验证 main_face_only——
   关: faces_tgt=2, faces_used=2 (全换旧行为)
   开: faces_tgt=2, faces_used=1, faces_boxes 只回主脸框(裁剪通道能咬合)
   06s 追加: main_face_hint 滞回——近等大(1.16×)在位者不被换主；真更大(≥1.3×)挑战者接管。
   直连 .104 生产引擎(带 service_auth 头,若有)。"""
import base64, json, sys, time
import cv2
import numpy as np
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # GBK 控制台打 ✓/✗ 不再崩
except Exception:
    pass

BASE = r"c:\模仿音色"
ENG = "http://192.168.0.104:8000"

img = cv2.imdecode(np.fromfile(BASE + r"\_warmup_face.jpg", dtype=np.uint8), cv2.IMREAD_COLOR)  # 中文路径需 fromfile
if img is None:
    print("FATAL: _warmup_face.jpg 读不到"); sys.exit(2)
h, w = img.shape[:2]


def make_canvas(scale):
    """左=原尺寸，右=缩小 scale 的同一张脸，同画布并排。返回 (b64, 画布)"""
    sm = cv2.resize(img, (int(w * scale), int(h * scale)))
    cv = np.full((h, w + sm.shape[1] + 40, 3), 40, np.uint8)
    cv[:h, :w] = img
    cv[20:20 + sm.shape[0], w + 20:w + 20 + sm.shape[1]] = sm
    _, b = cv2.imencode(".jpg", cv, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return base64.b64encode(b).decode(), cv


tgt_b64, canvas = make_canvas(0.55)          # 大小悬殊对(3.3×)：发现/接管用例
near_b64, near_canvas = make_canvas(0.93)    # 近等大对(1.16×)：滞回用例
_, sbuf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
src_b64 = base64.b64encode(sbuf).decode()

headers = {}
try:
    sys.path.insert(0, BASE)
    import service_auth
    headers = service_auth.client_headers()      # 若模块提供
except Exception:
    try:
        import service_auth
        tok = getattr(service_auth, "TOKEN", None) or getattr(service_auth, "_TOKEN", None)
        if tok:
            headers = {"X-Service-Token": tok}
    except Exception:
        pass

def call(mf, tgt=None, hint=None):
    p = {"source_image": src_b64, "target_image": tgt or tgt_b64, "enhance": "none"}
    if mf is not None:
        p["main_face_only"] = mf
    if hint is not None:
        p["main_face_hint"] = hint
    t0 = time.time()
    r = requests.post(ENG + "/faceswap", json=p, headers=headers, timeout=30)
    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:200]}")
        return None
    d = r.json()
    print(f"  faces_tgt={d.get('faces_tgt')} used={d.get('faces_used')} "
          f"boxes={len(d.get('faces_boxes') or [])} elapsed={d.get('elapsed_ms')}ms rtt={ms}ms")
    return d


def box_cx(d):
    bx = d["faces_boxes"][0]
    return (bx[0] + bx[2]) / 2


print("[1] main_face_only 未传(旧行为=全换):")
a = call(None)
print("[2] main_face_only=True (只换最大脸):")
b = call(True)

ok = (a and b
      and a.get("faces_tgt") == 2 and a.get("faces_used") == 2
      and b.get("faces_tgt") == 2 and b.get("faces_used") == 1
      and len(b.get("faces_boxes") or []) == 1)
# 主脸框应是大脸(左边那张,宽度≈原图脸框) —— 框中心 x 应落在左半张
if ok:
    cx = box_cx(b)
    ok = cx < w
    print(f"  主脸框中心x={int(cx)} (左图宽 {w}) → {'主脸=大脸 ✓' if ok else '选错脸 ✗'}")

# ── 06s 滞回：近等大画布(右脸 1.16× 悬殊不足 1.3×) ──
nh, nw = near_canvas.shape[:2]
right_cx = w + 20 + int(w * 0.93) // 2       # 右脸中心的近似 x（在位者=右脸）
print("[3] 近等大+hint指向右(小)脸 → 在位者不换主(滞回):")
c = call(True, tgt=near_b64, hint=[float(right_cx), float(20 + int(h * 0.93) // 2)])
if ok and c:
    cx = box_cx(c)
    ok = c.get("faces_used") == 1 and cx > w   # 主脸应保持在右半张
    print(f"  主脸框中心x={int(cx)} (>左图宽 {w} 即在位) → {'在位者保住 ✓' if ok else '被换主 ✗'}")
else:
    ok = False
print("[4] 悬殊对(3.3×)+hint指向右(小)脸 → 挑战者≥1.3×接管:")
d4 = call(True, hint=[float(w + 20 + int(w * 0.55) // 2), float(20 + int(h * 0.55) // 2)])
if ok and d4:
    cx = box_cx(d4)
    ok = d4.get("faces_used") == 1 and cx < w  # 大脸(左)接管
    print(f"  主脸框中心x={int(cx)} (<左图宽 {w} 即接管) → {'挑战者接管 ✓' if ok else '未接管 ✗'}")
else:
    ok = False
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
