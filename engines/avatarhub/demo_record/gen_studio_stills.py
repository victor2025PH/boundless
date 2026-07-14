# -*- coding: utf-8 -*-
"""换发型·定妆·试衣:直接调离线引擎在明星照片上出前后对比静图(比录 UI 稳)。
产物在 demo_record/studio/。makeup(8004,CPU快) / hair(8001,GPU) / tryon(8002,FitDiT重)。
"""
import base64
import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "studio")
os.makedirs(OUT, exist_ok=True)
STAR = r"C:\Users\user\Desktop\明星"


def b64_file(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def post(url, payload, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d, int((time.time() - t0) * 1000)


def save_b64(b64, name):
    path = os.path.join(OUT, name)
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    print("  saved", name, os.path.getsize(path) // 1024, "KB")
    return path


def do_makeup(src_b64, subject):
    for style in ["复古红唇", "元气桃花", "烟熏", "女团紫"]:
        try:
            d, ms = post("http://127.0.0.1:8004/makeup_transfer",
                         {"source_image": src_b64, "style": style})
            if d.get("result_image"):
                save_b64(d["result_image"], f"makeup_{subject}_{style}.jpg")
                print(f"  makeup {style} {ms}ms applied={d.get('applied')}")
        except Exception as e:
            print(f"  makeup {style} ERR {e}")


def do_hair(src_b64, subject, styles):
    for st in styles:
        try:
            # 先激活该发型,再用源图换发
            post("http://127.0.0.1:8001/hair_styles/switch", {"name": st}, timeout=15)
            d, ms = post("http://127.0.0.1:8001/hair_transfer",
                         {"source_image": src_b64, "paste_back": True}, timeout=180)
            if d.get("result_image"):
                save_b64(d["result_image"], f"hair_{subject}_{st}.jpg")
                print(f"  hair {st} {ms}ms pasted={d.get('pasted_back')}")
        except Exception as e:
            print(f"  hair {st} ERR {e}")


def do_tryon(person_b64, subject, clothes):
    for c in clothes:
        try:
            post("http://127.0.0.1:8002/clothes/switch", {"name": c}, timeout=15)
            d, ms = post("http://127.0.0.1:8002/tryon",
                         {"person_image": person_b64, "cloth_type": "upper"}, timeout=300)
            if d.get("result_image"):
                save_b64(d["result_image"], f"tryon_{subject}_{c}.jpg")
                print(f"  tryon {c} {ms}ms")
        except Exception as e:
            print(f"  tryon {c} ERR {e}")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "makeup"
    lyf = b64_file(os.path.join(STAR, "刘亦菲.jpg"))
    save_b64(lyf, "src_刘亦菲.jpg")
    if which in ("makeup", "all"):
        print("== 定妆 ==")
        do_makeup(lyf, "刘亦菲")
    if which in ("hair", "all"):
        print("== 发型 ==")
        do_hair(lyf, "刘亦菲", ["演示发型005", "演示发型012", "演示发型020"])
    if which in ("tryon", "all"):
        print("== 试衣 ==")
        lzl = b64_file(os.path.join(STAR, "林志玲.jpeg"))
        do_tryon(lzl, "林志玲", ["演示连衣裙001", "演示上衣010"])
    print("done")
