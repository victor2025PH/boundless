# -*- coding: utf-8 -*-
"""
DFM 训练素材采集/清洗管线（辨识度终极方案 · 阶段1）
====================================================
为「每角色专属 DFM 模型」准备干净的 src(名人)素材集。DeepFaceLab RTM 工作流只需
名人侧素材（dst 用公开 RTM 多样人脸集），故本管线聚焦：把一堆随手收集的
照片/视频 → 过滤成「确为本人、够大、够清晰、姿态多样、无重复」的训练帧。

用法：
  # 1) 把某角色的照片/视频丢进一个文件夹（jpg/png/mp4/mov 混放即可）
  # 2) 清洗（自动从 Hub 取该角色参考脸做身份过滤；Hub 不在线用 --ref 指定参考图目录）
  python dfm_material.py --char 刘德华 --src D:\素材\刘德华 --out dfm_workspace
  # 3) 只评估素材够不够（不写文件）
  python dfm_material.py --char 刘德华 --src D:\素材\刘德华 --probe

输出（--out 模式）：
  dfm_workspace/<角色>/keep/000123.jpg     清洗后全帧（喂给 DeepFaceLab extract）
  dfm_workspace/<角色>/rejects/<原因>/…    被拒帧（可人工复核捞回）
  dfm_workspace/<角色>/report.json         统计报告（数量/姿态/亮度/清晰度分布）

过滤规则（阈值均可命令行覆盖）：
  · 身份：与参考 embedding 余弦 ≥ 0.35（同人多视角实测 ≈0.6-0.9，异人 ≈0.1）
  · 尺寸：脸框长边 ≥ 224px（低于此训练价值低；384+ 最佳）
  · 清晰：脸区(缩到 256 后) Laplacian 方差 ≥ 40（真糊帧 ≈5-15，压缩正常照 ≈45+，高清 ≈100+）
  · 去重：与已保留帧 embedding 余弦 > 0.95 视为重复，保留清晰度更高的一张
  · 视频：默认每 6 帧抽 1（25/30fps ≈ 4-5 张/秒，配合去重足够）
素材达标线（DeepFaceLab 社区经验）：≥1500 张、yaw 覆盖 ±40°、含表情变化。
"""
import os, sys, json, argparse, base64, shutil, time
from pathlib import Path

import numpy as np
import cv2

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".ts", ".flv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def imread_u(p):
    """中文路径安全读图。"""
    try:
        return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def imwrite_u(p, img, q=95):
    cv2.imencode(Path(p).suffix, img, [cv2.IMWRITE_JPEG_QUALITY, q])[1].tofile(str(p))


def build_analyser(gpu: bool):
    from insightface.app import FaceAnalysis
    prov = ["CUDAExecutionProvider", "CPUExecutionProvider"] if gpu else ["CPUExecutionProvider"]
    fa = FaceAnalysis(name="buffalo_l", providers=prov)
    fa.prepare(ctx_id=0 if gpu else -1, det_size=(640, 640))
    return fa


def main_face(fa, img):
    faces = fa.get(img)
    if not faces:
        return None
    return max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))


def ref_embedding_from_hub(char: str):
    """从 Avatar Hub 取该角色主脸+gallery，平均成参考身份向量。"""
    import requests
    hub = os.environ.get("HUB_URL", "http://127.0.0.1:9000")
    r = requests.get(f"{hub}/profiles/{char}?include_face=true&include_gallery=true", timeout=10)
    r.raise_for_status()
    d = r.json()
    imgs = []
    if d.get("face_b64"):
        imgs.append(d["face_b64"])
    imgs += (d.get("face_gallery_b64") or [])
    return [cv2.imdecode(np.frombuffer(base64.b64decode(b), np.uint8), 1) for b in imgs]


def ref_embedding(fa, char: str, ref_dir: str | None):
    imgs = []
    if ref_dir:
        for p in Path(ref_dir).iterdir():
            if p.suffix.lower() in IMAGE_EXT:
                im = imread_u(p)
                if im is not None:
                    imgs.append(im)
    else:
        try:
            imgs = ref_embedding_from_hub(char)
        except Exception as e:
            print(f"[!] 无法从 Hub 取参考脸({e})；请用 --ref 指定参考图目录", file=sys.stderr)
            sys.exit(2)
    embs = []
    for im in imgs:
        f = main_face(fa, im)
        if f is not None:
            embs.append(f.normed_embedding)
    if not embs:
        print("[!] 参考图未检出人脸", file=sys.stderr)
        sys.exit(2)
    e = np.mean(np.stack(embs), axis=0)
    return e / (np.linalg.norm(e) + 1e-8), len(embs)


def iter_frames(src_dir: Path, video_step: int):
    """逐帧产出 (标识, 图像)。图片直接产出；视频按步长抽帧。"""
    files = sorted(src_dir.rglob("*"))
    for fp in files:
        ext = fp.suffix.lower()
        if ext in IMAGE_EXT:
            img = imread_u(fp)
            if img is not None:
                yield f"{fp.stem}", img
        elif ext in VIDEO_EXT:
            cap = cv2.VideoCapture(str(fp))
            idx = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if idx % video_step == 0:
                    yield f"{fp.stem}_f{idx:06d}", frame
                idx += 1
            cap.release()


def face_sharpness(img, bbox):
    """脸区清晰度（尺度不变）：脸 ROI 统一缩到 256 再算 Laplacian 方差，
    否则大脸/放大图的插值软化会把同等光学清晰度算出完全不同的数值。"""
    x1, y1, x2, y2 = [max(0, int(v)) for v in bbox]
    roi = img[y1:y2, x1:x2]
    if roi.size == 0:
        return 0.0
    g = cv2.cvtColor(cv2.resize(roi, (256, 256), interpolation=cv2.INTER_AREA),
                     cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def run(args):
    t0 = time.time()
    fa = build_analyser(gpu=not args.cpu)
    ref, n_ref = ref_embedding(fa, args.char, args.ref)
    print(f"[i] 参考身份向量就绪（{n_ref} 张参考图）")

    src_dir = Path(args.src)
    out_root = Path(args.out) / args.char if not args.probe else None
    keep_dir = rej_root = None
    if out_root:
        keep_dir = out_root / "keep"
        rej_root = out_root / "rejects"
        keep_dir.mkdir(parents=True, exist_ok=True)

    kept = []            # (embedding, sharpness, path)
    stats = {"total": 0, "no_face": 0, "not_person": 0, "too_small": 0,
             "blurry": 0, "dup": 0, "kept": 0}
    yaw_bins = {"<-25": 0, "-25..-10": 0, "-10..10": 0, "10..25": 0, ">25": 0}
    bright = []

    def reject(tag, key, img):
        stats[key] += 1
        if rej_root and args.save_rejects:
            d = rej_root / key
            d.mkdir(parents=True, exist_ok=True)
            imwrite_u(d / f"{tag}.jpg", img, q=85)

    for tag, img in iter_frames(src_dir, args.video_step):
        stats["total"] += 1
        f = main_face(fa, img)
        if f is None:
            reject(tag, "no_face", img); continue
        cos = float(np.dot(f.normed_embedding, ref))
        if cos < args.id_thresh:
            reject(tag, "not_person", img); continue
        w, h = f.bbox[2] - f.bbox[0], f.bbox[3] - f.bbox[1]
        if max(w, h) < args.min_face:
            reject(tag, "too_small", img); continue
        sharp = face_sharpness(img, f.bbox)
        if sharp < args.min_sharp:
            reject(tag, "blurry", img); continue
        # 去重：与已保留帧最大余弦 > dup_thresh 视为重复，保留更清晰的
        emb = f.normed_embedding
        dup_i = -1
        if kept:
            sims = np.stack([k[0] for k in kept]) @ emb
            j = int(np.argmax(sims))
            if float(sims[j]) > args.dup_thresh:
                dup_i = j
        if dup_i >= 0:
            if sharp > kept[dup_i][1] and keep_dir:
                # 新帧更清晰 → 替换旧的
                old = kept[dup_i][2]
                imwrite_u(old, img)
                kept[dup_i] = (emb, sharp, old)
            stats["dup"] += 1
            continue
        # 保留
        stats["kept"] += 1
        outp = (keep_dir / f"{stats['kept']:06d}.jpg") if keep_dir else None
        if outp is not None:
            imwrite_u(outp, img)
        kept.append((emb, sharp, outp))
        # 多样性统计
        yaw = float(f.pose[1]) if getattr(f, "pose", None) is not None else 0.0
        if yaw < -25: yaw_bins["<-25"] += 1
        elif yaw < -10: yaw_bins["-25..-10"] += 1
        elif yaw <= 10: yaw_bins["-10..10"] += 1
        elif yaw <= 25: yaw_bins["10..25"] += 1
        else: yaw_bins[">25"] += 1
        x1, y1, x2, y2 = [max(0, int(v)) for v in f.bbox]
        roi = img[y1:y2, x1:x2]
        if roi.size:
            bright.append(float(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).mean()))
        if stats["kept"] % 200 == 0:
            print(f"  … 已保留 {stats['kept']} / 扫过 {stats['total']}")

    # ── 报告 ──
    n = stats["kept"]
    side = yaw_bins["<-25"] + yaw_bins[">25"]
    verdict = []
    if n >= args.target_count:
        verdict.append(f"数量达标({n}≥{args.target_count})")
    else:
        verdict.append(f"数量不足({n}<{args.target_count})，建议补素材(访谈/多角度视频最佳)")
    if n and side / n < 0.10:
        verdict.append(f"侧脸偏少({side}/{n}={side/n:.0%}<10%)，建议补侧脸视频，否则直播转头会露馅")
    if bright:
        b = np.array(bright)
        if b.std() < 18:
            verdict.append("光照单一，建议补不同光照场景素材")
    report = {
        "char": args.char, "elapsed_s": round(time.time() - t0, 1),
        "stats": stats, "yaw_bins": yaw_bins,
        "brightness_mean": round(float(np.mean(bright)), 1) if bright else None,
        "brightness_std": round(float(np.std(bright)), 1) if bright else None,
        "thresholds": {"id": args.id_thresh, "min_face": args.min_face,
                       "min_sharp": args.min_sharp, "dup": args.dup_thresh},
        "verdict": verdict,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if out_root:
        (out_root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[✓] 清洗完成 → {keep_dir}（{n} 张）")
        print("    下一步：DeepFaceLab data_src=此目录 → extract(WF,512) → SAEHD/AMP 训练 → export .dfm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DFM 训练素材采集/清洗")
    ap.add_argument("--char", required=True, help="角色名（Hub 里的 profile 名）")
    ap.add_argument("--src", required=True, help="原始素材目录（照片/视频混放，递归扫描）")
    ap.add_argument("--out", default="dfm_workspace", help="输出根目录")
    ap.add_argument("--ref", default=None, help="参考图目录（不给则从 Hub 取该角色主脸+gallery）")
    ap.add_argument("--probe", action="store_true", help="只评估不落盘")
    ap.add_argument("--cpu", action="store_true", help="强制 CPU（默认试 GPU）")
    ap.add_argument("--video-step", type=int, default=6, dest="video_step", help="视频每 N 帧抽 1")
    ap.add_argument("--id-thresh", type=float, default=0.35, dest="id_thresh")
    ap.add_argument("--min-face", type=int, default=224, dest="min_face")
    ap.add_argument("--min-sharp", type=float, default=40.0, dest="min_sharp")
    ap.add_argument("--dup-thresh", type=float, default=0.95, dest="dup_thresh")
    ap.add_argument("--target-count", type=int, default=1500, dest="target_count")
    ap.add_argument("--save-rejects", action="store_true", default=True, dest="save_rejects")
    run(ap.parse_args())
