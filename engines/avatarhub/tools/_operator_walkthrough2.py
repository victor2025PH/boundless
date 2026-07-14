# -*- coding: utf-8 -*-
"""阶段13 运营走查第二轮：覆盖第一轮没走到的路径（临时角色全程隔离）。

第一轮走的是「主线」（建角→定妆包→试衣→换装→历史→回滚lookpack）；本轮走「支线」：
  ① 建角色绑脸 → ② 具名发型直选(hair_style 参数,非当前激活样式)
  → ③ 参考图妆容(ref_image_b64,非具名预设) → ④ 下装试穿(cloth_type=lower,传照)
  → ⑤ 连衣裙免传照(cloth_type=dresses,存照回退) → ⑥ 截图抠衣→入库→上身 E2E
  → ⑦ 回滚 tryon 版(第一轮只回滚过 lookpack) → ⑧ 微动 idle_video + 生效提示语核查
  → ⑨ 清理(角色+历史+存照+临时服装)。
每步计时并核对「出片即生效」新提示语——提示语误导运营就是 bug。"""
import base64
import sys
import time
from pathlib import Path

import requests


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HUB = "http://127.0.0.1:9000"
NAME = "_walkthrough2_支线"
TMP_CLOTH = "_walk2抠衣临时"
report: list[str] = []
t_all = time.time()


def step(label, cond, ms=0, extra=""):
    tag = "OK" if cond else "NG"
    line = f"[{tag}] {label}" + (f" {ms / 1000:.1f}s" if ms else "") + (f" {extra}" if extra else "")
    print("  " + line)
    report.append(line)
    return cond


face_img = sorted(Path(r"c:\模仿音色\hair_styles").glob("演示发型*.jpg"))[8]
ref_face = sorted(Path(r"c:\模仿音色\hair_styles").glob("演示发型*.jpg"))[20]
person_img = sorted(Path(r"C:\datasets\viton_hd_test\image").glob("*.jpg"))[22]
worn_img = sorted(Path(r"C:\datasets\viton_hd_test\image").glob("*.jpg"))[33]
face_b64 = base64.b64encode(face_img.read_bytes()).decode()
ref_b64 = base64.b64encode(ref_face.read_bytes()).decode()
person_b64 = base64.b64encode(person_img.read_bytes()).decode()
worn_b64 = base64.b64encode(worn_img.read_bytes()).decode()

for pre in (lambda: requests.delete(f"{HUB}/profiles/{NAME}", timeout=10),
            lambda: requests.post(f"{HUB}/api/tryon/delete_cloth",
                                  json={"name": TMP_CLOTH}, timeout=10)):
    try:
        pre()
    except Exception:
        pass

try:
    # ① 建角色
    t = time.time()
    r = requests.post(f"{HUB}/profiles", json={"name": NAME, "face_b64": face_b64}, timeout=15)
    step("① 建角色绑脸", r.status_code == 200, (time.time() - t) * 1000)

    # ② 具名发型直选（非 8001 当前激活样式——验证 activate+transfer 链）
    t = time.time()
    r = requests.post(f"{HUB}/api/profiles/{NAME}/hair_preset",
                      json={"hair_style": "演示发型025", "apply": True}, timeout=240)
    j = r.json() if r.status_code == 200 else {}
    step("② 具名发型直选(演示发型025)", r.status_code == 200 and j.get("ok"),
         (time.time() - t) * 1000, str(j.get("detail") or j.get("hint") or "")[:60])

    # ③ 参考图妆容（不给 style 给 ref_image_b64——妆容提取→迁移路径）
    t = time.time()
    r = requests.post(f"{HUB}/api/profiles/{NAME}/makeup_preset",
                      json={"ref_image_b64": ref_b64, "apply": True}, timeout=120)
    j = r.json() if r.status_code == 200 else {}
    step("③ 参考图妆容(ref_image)", r.status_code == 200 and j.get("ok"),
         (time.time() - t) * 1000, f"style={j.get('makeup_style', '?')}")

    # ④ 下装试穿（cloth_type=lower，传全身照→顺手存档）
    t = time.time()
    r = requests.post(f"{HUB}/api/profiles/{NAME}/tryon_preset",
                      json={"person_image_b64": person_b64, "cloth_name": "演示裤装001",
                            "cloth_type": "lower", "field": "body_video", "animate": "off"},
                      timeout=300)
    j4 = r.json() if r.status_code == 200 else {}
    step("④ 下装试穿(lower,传照)", r.status_code == 200 and j4.get("ok"),
         (time.time() - t) * 1000, str(j4.get("hint") or j4.get("detail") or "")[:70])

    # ⑤ 连衣裙免传照（cloth_type=dresses，走存照回退）
    t = time.time()
    r = requests.post(f"{HUB}/api/profiles/{NAME}/tryon_preset",
                      json={"cloth_name": "演示连衣裙001", "cloth_type": "dresses",
                            "field": "body_video", "animate": "off"}, timeout=300)
    j5 = r.json() if r.status_code == 200 else {}
    step("⑤ 连衣裙免传照(dresses)", r.status_code == 200 and j5.get("ok"),
         (time.time() - t) * 1000)
    # 提示语核查：非激活角色 → 应说「激活该角色后第一句口型即用新底片」，不能再喊重新激活待机
    hint5 = str(j5.get("hint") or "")
    step("⑤b 生效提示语(body_video)", "第一句口型即用新底片" in hint5, extra=hint5[:60])

    # ⑥ 截图抠衣→入库→上身 E2E（穿着照走人体解析路径）
    t = time.time()
    r = requests.post(f"{HUB}/api/tryon/extract_cloth",
                      json={"image": worn_b64, "save_name": TMP_CLOTH, "part": "upper"},
                      timeout=120)
    j = r.json() if r.status_code == 200 else {}
    ok6a = r.status_code == 200 and (j.get("ok") or j.get("saved") or j.get("result_image"))
    step("⑥ 截图抠衣入库", bool(ok6a), (time.time() - t) * 1000,
         str(j.get("detail") or "")[:60])
    if ok6a:
        t = time.time()
        r = requests.post(f"{HUB}/api/tryon/preview",
                          json={"profile": NAME, "cloth_name": TMP_CLOTH,
                                "cloth_type": "upper"}, timeout=300)
        j = r.json() if r.status_code == 200 else {}
        step("⑥b 抠出的衣服可上身", r.status_code == 200 and bool(j.get("result_image")),
             (time.time() - t) * 1000)
    else:
        step("⑥b 抠出的衣服可上身", False, extra="抠衣失败跳过")

    # ⑦ 回滚 tryon 版（第一轮只回滚过 lookpack；tryon 回滚走视频字段分支）
    ents = requests.get(f"{HUB}/api/profiles/{NAME}/look_history", timeout=10).json().get("entries") or []
    tr = next((e for e in ents if e["kind"] == "tryon" and "裤装" in str(e.get("meta", {}).get("style", ""))), None)
    if tr:
        r = requests.post(f"{HUB}/api/profiles/{NAME}/look_history/{tr['id']}/restore", timeout=15)
        j = r.json() if r.status_code == 200 else {}
        ok7 = (r.status_code == 200 and j.get("ok")
               and "body_video" in (j.get("applied") or [])
               and "tryon_image" in (j.get("applied") or []))
        step("⑦ 回滚tryon版(裤装)", ok7, extra=f"applied={j.get('applied')} hint={str(j.get('hint'))[:40]}")
    else:
        step("⑦ 回滚tryon版(裤装)", False, extra=f"历史无裤装tryon条目 kinds={[e['kind'] for e in ents]}")

    # ⑧ 微动 idle_video + 提示语（非激活+vcam关 → 应兜底说重新激活待机生效）
    t = time.time()
    r = requests.post(f"{HUB}/api/profiles/{NAME}/idle_motion",
                      json={"source": "auto", "secs": 4, "field": "idle_video"}, timeout=180)
    j = r.json() if r.status_code == 200 else {}
    step("⑧ 微动idle_video", r.status_code == 200 and j.get("ok"), (time.time() - t) * 1000,
         f"来源={j.get('source')} hint={str(j.get('hint'))[:45]}")
finally:
    # ⑨ 清理：临时服装 + 角色（连带历史/存照）
    try:
        requests.post(f"{HUB}/api/tryon/delete_cloth", json={"name": TMP_CLOTH}, timeout=10)
    except Exception:
        pass
    requests.delete(f"{HUB}/profiles/{NAME}", timeout=10)
    hist_gone = not (Path(r"c:\模仿音色\data\look_history") / NAME).exists()
    photo_gone = not (Path(r"c:\模仿音色\data\body_photo") / f"{NAME}.jpg").exists()
    step("⑨ 清理(衣+角色+历史+存照)", hist_gone and photo_gone,
         extra=f"hist_gone={hist_gone} photo_gone={photo_gone}")

ok_n = sum(1 for l in report if l.startswith("[OK]"))
print(f"\n[WALKTHROUGH2] {ok_n}/{len(report)} 步通过，总耗时 {time.time() - t_all:.0f}s")
sys.exit(0 if ok_n == len(report) else 1)
