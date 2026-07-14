# -*- coding: utf-8 -*-
"""妆容服务端到端验证：取角色库真实人脸 → 全预设上妆 + 参考图提取模式 → 落盘对比图。
产物: logs/look_pack_impl_20260708/  （原图/各预设结果/掩码可视化/参考提取结果）"""
import base64, json, sys, urllib.request, urllib.parse
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HUB = "http://127.0.0.1:9000"
MK = "http://127.0.0.1:8004"
OUT = Path(r"c:\模仿音色\logs\look_pack_impl_20260708")
OUT.mkdir(parents=True, exist_ok=True)


def post(url, body, timeout=60):
    req = urllib.request.Request(url, json.dumps(body).encode("utf-8"),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def get(url, timeout=15):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def face_of(name):
    d = get(f"{HUB}/profiles/{urllib.parse.quote(name)}?include_face=true")
    return d.get("face_b64", "")


def save(name, b64):
    (OUT / name).write_bytes(base64.b64decode(b64))
    print(f"  saved {name}")


src = face_of("Inside")
assert src, "Inside 无人脸"
save("00_原图_Inside.jpg", src)

styles = get(f"{MK}/makeup_styles")
print("可用预设:", styles["presets"])

for st in styles["presets"]:
    r = post(f"{MK}/makeup_transfer",
             {"source_image": src, "style": st, "debug": st == "自然裸妆"})
    save(f"01_{st}.jpg", r["result_image"])
    if r.get("debug_masks"):
        save(f"01_{st}_掩码.jpg", r["debug_masks"])
    print(f"  {st}: {r['elapsed_ms']}ms applied={list(r['applied'].keys())}")

# 参考图模式：用刘亦菲照片作为妆容参考提取色彩
ref = face_of("刘亦菲")
if ref:
    save("02_参考_刘亦菲.jpg", ref)
    ext = post(f"{MK}/makeup_extract", {"image": ref})
    print("提取:", {k: v for k, v in ext.items() if k != "ok"})
    r = post(f"{MK}/makeup_transfer", {"source_image": src, "ref_image": ref})
    save("02_参考迁移结果.jpg", r["result_image"])
    print(f"  参考迁移: {r['elapsed_ms']}ms applied={list(r['applied'].keys())}")

print("\n全部产物 →", OUT)
