# -*- coding: utf-8 -*-
"""阶段10 出片历史线上冒烟：临时角色走 记录→列表→缩略图→回滚→删除 全闭环。
妆容服务(8004)在线则用真 makeup_preset 触发记录；离线则直接落一条 idle 类历史
（用 API 无法伪造——那就退化为只测列表/回滚端点的 404 行为）。跑完清理角色。"""
import base64
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HUB = "http://127.0.0.1:9000"
NAME = "_lookhist_smoke"
ok_n, ng_n = 0, 0


def check(label, cond, extra=""):
    global ok_n, ng_n
    print(f"  [{'OK' if cond else 'NG'}] {label} {extra}")
    ok_n, ng_n = ok_n + (1 if cond else 0), ng_n + (0 if cond else 1)


# 0) 建临时角色（绑一张演示人脸）——角色 CRUD 走 /profiles（历史端点才在 /api 下）
from pathlib import Path
face = sorted(Path(r"c:\模仿音色\hair_styles").glob("演示发型*.jpg"))[0]
b64 = base64.b64encode(face.read_bytes()).decode()
try:
    requests.delete(f"{HUB}/profiles/{NAME}", timeout=10)
except Exception:
    pass
r = requests.post(f"{HUB}/profiles", json={"name": NAME, "face_b64": b64,
                                           "description": "出片历史冒烟"}, timeout=15)
check("临时角色建立+绑脸", r.status_code == 200, f"({r.status_code})")

try:
    # 1) 触发一次真实出片（makeup_preset，8004 在线时 1-3s）
    mk = requests.get(f"{HUB}/api/makeup/styles", timeout=6).json()
    if mk.get("up"):
        r = requests.post(f"{HUB}/api/profiles/{NAME}/makeup_preset",
                          json={"style": (mk.get("presets") or ["自然裸妆"])[0]}, timeout=60)
        check("makeup_preset 出片", r.status_code == 200 and r.json().get("ok"),
              f"({r.status_code})")
        # 连点去重：同参数再跑一次不应新增条目
        requests.post(f"{HUB}/api/profiles/{NAME}/makeup_preset",
                      json={"style": (mk.get("presets") or ["自然裸妆"])[0]}, timeout=60)
    else:
        print("  [SKIP] 8004 离线，跳过真实出片（历史链后续步骤将空转）")

    # 2) 列表
    r = requests.get(f"{HUB}/api/profiles/{NAME}/look_history", timeout=10)
    j = r.json()
    entries = j.get("entries") or []
    check("历史列表", r.status_code == 200 and j.get("ok"), f"共 {len(entries)} 条")
    if mk.get("up"):
        check("出片自动记录", len(entries) >= 1,
              f"kind={entries[0].get('kind') if entries else '-'}")
        check("连点去重(同图不重复入档)", len(entries) == 1, f"实际 {len(entries)} 条")

    if entries:
        hid = entries[0]["id"]
        # 3) 缩略图 + 原图
        t = requests.get(f"{HUB}/api/profiles/{NAME}/look_history/{hid}/image",
                         params={"thumb": 1}, timeout=10)
        f = requests.get(f"{HUB}/api/profiles/{NAME}/look_history/{hid}/image", timeout=10)
        check("缩略图(thumb=1)", t.status_code == 200 and t.headers.get("content-type") == "image/jpeg"
              and len(t.content) < len(f.content), f"thumb {len(t.content)}B < full {len(f.content)}B")

        # 4) 回滚：restore 后 face_styled_b64 应等于历史那张（覆盖被找回）
        r = requests.post(f"{HUB}/api/profiles/{NAME}/look_history/{hid}/restore", timeout=15)
        jj = r.json()
        check("一键回滚", r.status_code == 200 and jj.get("ok"),
              f"applied={jj.get('applied')}")
        p = requests.get(f"{HUB}/profiles/{NAME}",
                         params={"include_face": "true"}, timeout=10).json()
        hist_b64 = base64.b64encode(
            (Path(r"c:\模仿音色\data\look_history") / NAME / f"{hid}.jpg").read_bytes()).decode()
        check("回滚后 face_styled_b64 == 历史图",
              (p.get("face_styled_b64") or "")[:512] == hist_b64[:512]
              and bool(p.get("use_styled_face")))

        # 5) 删单条
        r = requests.post(f"{HUB}/api/profiles/{NAME}/look_history/{hid}/delete", timeout=10)
        check("删单条", r.status_code == 200 and r.json().get("ok"),
              f"剩 {r.json().get('left')}")
        r = requests.get(f"{HUB}/api/profiles/{NAME}/look_history/{hid}/image", timeout=10)
        check("删后图 404", r.status_code == 404)

    # 6) 非法 id 防穿越
    r = requests.get(f"{HUB}/api/profiles/{NAME}/look_history/..%2f..%2fsecrets/image", timeout=10)
    check("非法 id 拒绝", r.status_code in (400, 404, 422), f"({r.status_code})")
finally:
    requests.delete(f"{HUB}/profiles/{NAME}", timeout=10)
    import shutil
    shutil.rmtree(Path(r"c:\模仿音色\data\look_history") / NAME, ignore_errors=True)
    print(f"[清理] 临时角色与历史目录已删")

print(f"\n[SMOKE] {ok_n} OK / {ng_n} NG")
sys.exit(1 if ng_n else 0)
