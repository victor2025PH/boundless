# -*- coding: utf-8 -*-
"""相册策展器：原始套图 → 去重 → VLM 场景分析 → 语义改名 → 人设相册 + manifest。

用法（一次性策展 + 可重复增量）：
    python tools/album_curator.py --src "D:/图库/小生套可爱系列95" \
        --persona lin_xiaoyu --album-root <data>/config/persona_albums \
        --vlm http://192.168.0.176:11434/v1 --model qwen2.5vl:7b

产出：
  album_root/<persona>/
    face_ref.jpg                     # 评分最高的正脸定妆照（PuLID 锁脸/img2img 基础图）
    <scene>_<series>_<nn>.jpg        # 场景化命名的相册图
    manifest.json                    # 每张图完整元数据（场景/触发词/配文/系列/来源）
  32 个 mp4 同样入 manifest（media_type=video，供注册相册按触发词调用）。

设计要点：
- sha256 去重：套图常见 "1 (1).jpg"/"1 (1)(1).jpg" 完全同字节 → 只保留一份。
- VLM 每图输出 JSON：scene（英文短标签）/ description / outfit（服装聚类=子系列）/
  face_ref_score（0-10 正脸清晰度）/ triggers（中文触发词）/ caption（人设口吻配文）。
- 系列（series）= outfit 聚类：同服装连拍视觉高度相似，发过其一再发其二像复读机。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_PROMPT = """分析这张人物照片，只输出 JSON（不要 markdown 代码块），字段：
{
 "scene": "场景英文短标签，小写连字符，如 bedroom/cafe/street/mirror-selfie/car/office/outdoor-park/restaurant",
 "description": "一句中文描述画面（人物姿态/着装/环境）",
 "outfit": "服装英文短标签（颜色+类型），如 white-dress/pink-hoodie/black-suspender，同套衣服务必用同一标签",
 "face_ref_score": 0到10的整数，作为人脸参考照的适合度（正脸、清晰、无遮挡、光线好=高分；侧脸/远景/遮挡=低分,
 "triggers": ["2-4个中文触发词，客户聊到什么话题适合发这张，如 自拍/睡觉/咖啡/逛街"],
 "caption": "以照片中女生第一人称发这张照片时的自然配文，10-20字，口语化带一点俏皮"
}"""


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def vlm_analyze(vlm_base: str, model: str, img_path: Path,
                timeout: float = 60.0, retries: int = 2) -> dict:
    b64 = base64.b64encode(img_path.read_bytes()).decode()
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "max_tokens": 400,
        "temperature": 0.1,
    }
    last_err = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(
                vlm_base.rstrip("/") + "/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer ollama"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            txt = data["choices"][0]["message"]["content"] or ""
            m = re.search(r"\{[\s\S]*\}", txt)
            if not m:
                raise ValueError(f"no json in: {txt[:120]}")
            out = json.loads(m.group(0))
            out["scene"] = re.sub(r"[^a-z0-9-]", "-",
                                  str(out.get("scene") or "misc").lower())[:24]
            out["outfit"] = re.sub(r"[^a-z0-9-]", "-",
                                   str(out.get("outfit") or "misc").lower())[:24]
            out["face_ref_score"] = max(0, min(10, int(out.get("face_ref_score") or 0)))
            if not isinstance(out.get("triggers"), list):
                out["triggers"] = []
            # VLM 偶发把多个触发词连写成一个串（"自拍/化妆/拍照"）——匹配语义是
            # "关键词 in 客户文本"，整串永远命不中 → 按常见分隔符拆开。
            _flat = []
            for t in out["triggers"]:
                _flat += re.split(r"[/、,，;；|]+", str(t))
            out["triggers"] = [t.strip()[:12] for t in _flat if t.strip()][:6]
            out["caption"] = str(out.get("caption") or "")[:40]
            out["description"] = str(out.get("description") or "")[:120]
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5)
    raise RuntimeError(f"vlm analyze failed: {last_err}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--persona", required=True)
    ap.add_argument("--album-root", required=True)
    ap.add_argument("--vlm", default="http://192.168.0.176:11434/v1")
    ap.add_argument("--model", default="qwen2.5vl:7b")
    ap.add_argument("--limit", type=int, default=0, help="调试用：只处理前 N 张")
    args = ap.parse_args()

    src = Path(args.src)
    dst = Path(args.album_root) / args.persona
    dst.mkdir(parents=True, exist_ok=True)

    # 1) 收集 + sha 去重（jpg 与 mp4 分开；成对素材 "N (i).jpg/.mp4" 共享编号）
    seen: dict = {}
    jpgs, mp4s = [], []
    for f in sorted(src.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".mp4"):
            continue
        digest = sha256_file(f)
        if digest in seen:
            continue
        seen[digest] = f
        (mp4s if ext == ".mp4" else jpgs).append((f, digest))
    print(f"[curator] 源 {len(list(src.iterdir()))} 项 → 去重后 jpg={len(jpgs)} mp4={len(mp4s)}")

    if args.limit:
        jpgs = jpgs[: args.limit]

    # 2) VLM 逐张分析
    records = []
    t0 = time.time()
    for i, (f, digest) in enumerate(jpgs, 1):
        try:
            meta = vlm_analyze(args.vlm, args.model, f)
        except Exception as e:  # noqa: BLE001
            print(f"[curator] {f.name} 分析失败: {e}")
            meta = {"scene": "misc", "description": "", "outfit": "misc",
                    "face_ref_score": 0, "triggers": [], "caption": ""}
        records.append({"src": str(f), "sha256": digest, **meta})
        if i % 10 == 0 or i == len(jpgs):
            print(f"[curator] VLM {i}/{len(jpgs)} ({time.time()-t0:.0f}s)")

    # 3) 选 face_ref（最高分；平分取靠前）
    face = max(records, key=lambda r: r["face_ref_score"]) if records else None

    # 4) 场景化改名 + 拷贝（scene_outfit 分组内编号）
    counters: dict = {}
    for r in records:
        key = f"{r['scene']}_{r['outfit']}"
        counters[key] = counters.get(key, 0) + 1
        new_name = f"{key}_{counters[key]:02d}.jpg"
        r["file"] = new_name
        shutil.copy2(r["src"], dst / new_name)
    if face is not None:
        shutil.copy2(face["src"], dst / "face_ref.jpg")
        print(f"[curator] face_ref = {face['file']} (score={face['face_ref_score']}) "
              f"desc={face['description']}")

    # 5) mp4 直接按序拷贝入库（视频不做 VLM 帧分析——manifest 标注 series=video）
    vids = []
    for i, (f, digest) in enumerate(mp4s, 1):
        new_name = f"video_{i:02d}.mp4"
        shutil.copy2(f, dst / new_name)
        vids.append({"file": new_name, "src": str(f), "sha256": digest,
                     "media_type": "video"})

    manifest = {
        "persona": args.persona,
        "curated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_dir": str(src),
        "face_ref": (face or {}).get("file", ""),
        "photos": [{k: r[k] for k in
                    ("file", "scene", "outfit", "description", "triggers",
                     "caption", "face_ref_score", "sha256")} for r in records],
        "videos": vids,
    }
    (dst / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    # 6) 汇总
    from collections import Counter
    print("[curator] 场景分布:", dict(Counter(r["scene"] for r in records)))
    print("[curator] 服装系列:", dict(Counter(r["outfit"] for r in records)))
    print(f"[curator] 完成 → {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
