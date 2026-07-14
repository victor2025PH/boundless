# -*- coding: utf-8 -*-
"""06v E2E：离席画面设置持久链路(对运行中 hub 实测)。
   [1] /api/bg_images 列出品牌图  [2] POST effect_cfg 存 away 三键+回读
   [3] 越界拒绝(路径穿越/超长)   [4] 注入 dry-run: 现场 import avatar_hub 太重,
       改为读回 data/effect_cfg.json 直接喂 _away_env_from_cfg 的门禁已覆盖,此处只验持久层。
   [5] 复原：把 away 三键恢复默认(blur/默认文案/空图),不留测试残留。
"""
import sys

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HUB = "http://127.0.0.1:9000"
v = {}

j = requests.get(f"{HUB}/api/bg_images", timeout=5).json()
v["bg_images 列表"] = j.get("ok") is True and "_smoke_bg.jpg" in (j.get("images") or [])
print(f"[1] bg_images: {j.get('images')}")

j = requests.post(f"{HUB}/api/effect_cfg", json={
    "awayStyle": "image", "awayText": "去去就回 · BRB", "awayImage": "_smoke_bg.jpg"}, timeout=5).json()
c = j.get("cfg") or {}
v["存三键+回读"] = (j.get("ok") is True and c.get("awayStyle") == "image"
                    and c.get("awayText") == "去去就回 · BRB" and c.get("awayImage") == "_smoke_bg.jpg")
print(f"[2] 保存回读: style={c.get('awayStyle')} text={c.get('awayText')!r} image={c.get('awayImage')}")

j1 = requests.post(f"{HUB}/api/effect_cfg", json={"awayImage": r"..\..\sam.jpg"}, timeout=5).json()
j2 = requests.post(f"{HUB}/api/effect_cfg", json={"awayText": "长" * 41}, timeout=5).json()
j3 = requests.post(f"{HUB}/api/effect_cfg", json={"awayStyle": "rainbow"}, timeout=5).json()
v["越界整单拒绝"] = all(x.get("ok") is False for x in (j1, j2, j3))
print(f"[3] 拒绝: 路径穿越={j1.get('ok')} 超长={j2.get('ok')} 非法style={j3.get('ok')}")

g = requests.get(f"{HUB}/api/effect_cfg", timeout=5).json().get("cfg") or {}
v["拒绝后原值未污染"] = g.get("awayImage") == "_smoke_bg.jpg" and g.get("awayText") == "去去就回 · BRB"
print(f"[4] 拒绝后读回: image={g.get('awayImage')} (应仍为 _smoke_bg.jpg)")

j = requests.post(f"{HUB}/api/effect_cfg", json={
    "awayStyle": "blur", "awayText": "稍等片刻 · Be right back", "awayImage": ""}, timeout=5).json()
v["复原默认"] = j.get("ok") is True and (j.get("cfg") or {}).get("awayStyle") == "blur"
print(f"[5] 复原: {j.get('ok')}")

print(f"结论: {v}")
sys.exit(0 if all(v.values()) and len(v) == 5 else 1)
