# -*- coding: utf-8 -*-
"""阶段12 运营式全链走查：模拟主播开播前的完整操作序列（临时角色全程隔离）。

走查序列（对齐 UI 操作路径，不是端点单测）：
  ① 建角色绑脸 → ② 一键定妆包(真发型+妆容——空闲机首次发型步不被显存闸跳过)
  → ③ 试衣写底片(传全身照，验证顺手存档) → ④ 换装二连(不传照，验证全身照记忆)
  → ⑤ 出片历史应有 lookpack+tryon 多条 → ⑥ 回滚到定妆包版 → ⑦ 对比数据完备性
  → ⑧ 清理（角色+历史+存照随删）。
每步计时，产出走查报告——摩擦点就是下阶段的优化清单。"""
import base64
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HUB = "http://127.0.0.1:9000"
NAME = "_walkthrough_运营走查"
report: list[str] = []
t_all = time.time()


def step(label, cond, ms=0, extra=""):
    tag = "OK" if cond else "NG"
    line = f"[{tag}] {label}" + (f" {ms / 1000:.1f}s" if ms else "") + (f" {extra}" if extra else "")
    print("  " + line)
    report.append(line)
    return cond


face_img = sorted(Path(r"c:\模仿音色\hair_styles").glob("演示发型*.jpg"))[5]
person_img = sorted(Path(r"C:\datasets\viton_hd_test\image").glob("*.jpg"))[10]
face_b64 = base64.b64encode(face_img.read_bytes()).decode()
person_b64 = base64.b64encode(person_img.read_bytes()).decode()

try:
    requests.delete(f"{HUB}/profiles/{NAME}", timeout=10)
except Exception:
    pass

try:
    # ① 建角色
    t = time.time()
    r = requests.post(f"{HUB}/profiles", json={"name": NAME, "face_b64": face_b64}, timeout=15)
    step("① 建角色绑脸", r.status_code == 200, (time.time() - t) * 1000)

    # ② 一键定妆包：真发型（use_hair→8001 当前激活样式）+ 妆容
    t = time.time()
    r = requests.post(f"{HUB}/api/profiles/{NAME}/look_pack",
                      json={"use_hair": True, "makeup_style": "斩男红梨", "apply": True},
                      timeout=240)
    j = r.json() if r.status_code == 200 else {}
    st = j.get("steps") or {}
    hair_ok = bool(st.get("hair", {}).get("ok"))
    mk_ok = bool(st.get("makeup", {}).get("ok"))
    step("② 一键定妆包", r.status_code == 200 and j.get("ok"), (time.time() - t) * 1000,
         f"发型={'✓' if hair_ok else '✗' + str(st.get('hair', {}).get('error', ''))[:60]} "
         f"妆容={'✓' if mk_ok else '✗' + str(st.get('makeup', {}).get('error', ''))[:60]}")

    # ③ 试衣写底片（传全身照——Hub 应顺手存档）
    t = time.time()
    r = requests.post(f"{HUB}/api/profiles/{NAME}/tryon_preset",
                      json={"person_image_b64": person_b64, "cloth_name": "演示上衣001",
                            "cloth_type": "upper", "field": "body_video"}, timeout=300)
    j = r.json() if r.status_code == 200 else {}
    step("③ 试衣写底片(传照)", r.status_code == 200 and j.get("ok"), (time.time() - t) * 1000,
         f"微动={'✓' if j.get('animated') else '静帧'}")
    d = requests.get(f"{HUB}/profiles/{NAME}", timeout=10).json()
    step("③b 全身照已顺手存档", bool(d.get("has_body_photo")))

    # ④ 换装二连：不传照（全身照记忆应接管）
    t = time.time()
    r = requests.post(f"{HUB}/api/profiles/{NAME}/tryon_preset",
                      json={"cloth_name": "演示上衣002", "cloth_type": "upper",
                            "field": "body_video", "animate": "off"}, timeout=300)
    j = r.json() if r.status_code == 200 else {}
    step("④ 换装免重传(存照回退)", r.status_code == 200 and j.get("ok"), (time.time() - t) * 1000)

    # ⑤ 出片历史盘点
    ents = requests.get(f"{HUB}/api/profiles/{NAME}/look_history", timeout=10).json().get("entries") or []
    kinds = [e["kind"] for e in ents]
    step("⑤ 历史自动存档", "lookpack" in kinds and kinds.count("tryon") >= 2,
         extra=f"共{len(ents)}条 {kinds}")

    # ⑥ 回滚到定妆包版（换装折腾后一键找回定妆脸）
    lp = next((e for e in ents if e["kind"] == "lookpack"), None)
    if lp:
        r = requests.post(f"{HUB}/api/profiles/{NAME}/look_history/{lp['id']}/restore", timeout=15)
        step("⑥ 回滚定妆包版", r.status_code == 200 and r.json().get("ok"),
             extra=f"applied={r.json().get('applied')}")
    else:
        step("⑥ 回滚定妆包版", False, extra="没有 lookpack 条目")

    # ⑦ 对比模式数据完备性（前端纯展示，这里验两条目图都可取）
    if len(ents) >= 2:
        a, b = ents[0]["id"], ents[1]["id"]
        ra = requests.get(f"{HUB}/api/profiles/{NAME}/look_history/{a}/image", timeout=10)
        rb = requests.get(f"{HUB}/api/profiles/{NAME}/look_history/{b}/image", timeout=10)
        step("⑦ 对比两图可取", ra.status_code == 200 and rb.status_code == 200,
             extra=f"{len(ra.content) // 1024}KB / {len(rb.content) // 1024}KB")
finally:
    # ⑧ 清理：删角色应连带清历史与存照
    requests.delete(f"{HUB}/profiles/{NAME}", timeout=10)
    hist_gone = not (Path(r"c:\模仿音色\data\look_history") / NAME).exists()
    photo_gone = not (Path(r"c:\模仿音色\data\body_photo") / f"{NAME}.jpg").exists()
    step("⑧ 删角色连带清历史+存照", hist_gone and photo_gone,
         extra=f"hist_gone={hist_gone} photo_gone={photo_gone}")

ok_n = sum(1 for l in report if l.startswith("[OK]"))
print(f"\n[WALKTHROUGH] {ok_n}/{len(report)} 步通过，总耗时 {time.time() - t_all:.0f}s")
sys.exit(0 if ok_n == len(report) else 1)
