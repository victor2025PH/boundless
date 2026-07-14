# -*- coding: utf-8 -*-
"""
DFM 训练素材 —— 人脸提取/对齐（辨识度终极方案 · 阶段2）
=========================================================
把 dfm_material.py 清洗出的干净全帧（keep/）→ DeepFaceLab 可直接吃的「对齐脸集」。
产出 = 标准 JPG（对齐到 WHOLE_FACE，默认 512）+ 内嵌 DFLJPG 元数据（APP15/0xEF 段里
pickle 的 dict：face_type / landmarks / source_rect / source_landmarks / image_to_face_mat），
与 DeepFaceLab 官方 extractor 产出的 aligned 图逐字段同构 → 直接 data_src/aligned 用，
**跳过 DFL 自带 extractor**（后者在 Blackwell/5090 上还得先解决 TF/ORT，本管线用 insightface 绕开）。

对齐口径逐行对齐 DeepFaceLab LandmarksProcessor.get_transform_mat(WHOLE_FACE)：
  68 点子集(17:49 + 54) umeyama → landmarks_2D_new，padding 0.40 + 额头上抬 7%。
（这套 mat 与我们 faceswap_api 里 DFMSwap 推理用的完全一致——训练/推理对齐同源，零偏移。）

用法：
  python dfm_extract.py --char 刘德华 --keep dfm_workspace\刘德华\keep --size 512
  # 直接对一个图片夹提取（跳过 material 清洗，自测/小样用）：
  python dfm_extract.py --char X --keep <图片夹> --size 512 --no-id-filter
输出：
  <keep 同级>\aligned\*.jpg    对齐脸集（DFLJPG，喂 DeepFaceLab）
  <keep 同级>\aligned\_faceset_report.json
"""
import os, sys, json, argparse, pickle, struct, time
from pathlib import Path
import numpy as np
import cv2

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# ── DeepFaceLab WHOLE_FACE 对齐（与 faceswap_api._dfl_whole_face_mat 同源）──
_L2D_NEW = np.array([
    [0.000213256,0.106454],[0.0752622,0.038915],[0.18113,0.0187482],[0.29077,0.0344891],[0.393397,0.0773906],
    [0.586856,0.0773906],[0.689483,0.0344891],[0.799124,0.0187482],[0.904991,0.038915],[0.98004,0.106454],
    [0.490127,0.203352],[0.490127,0.307009],[0.490127,0.409805],[0.490127,0.515625],
    [0.36688,0.587326],[0.426036,0.609345],[0.490127,0.628106],[0.554217,0.609345],[0.613373,0.587326],
    [0.121737,0.216423],[0.187122,0.178758],[0.265825,0.179852],[0.334606,0.231733],[0.260918,0.245099],[0.182743,0.244077],
    [0.645647,0.231733],[0.714428,0.179852],[0.793132,0.178758],[0.858516,0.216423],[0.79751,0.244077],[0.719335,0.245099],
    [0.254149,0.780233],[0.726104,0.780233]], dtype=np.float32)


def imread_u(p):
    try:
        return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def umeyama(src, dst, estimate_scale=True):
    num, dim = src.shape
    sm, dm = src.mean(0), dst.mean(0)
    sd, dd = src - sm, dst - dm
    A = dd.T @ sd / num
    d = np.ones((dim,))
    if np.linalg.det(A) < 0:
        d[dim - 1] = -1
    T = np.eye(dim + 1)
    U, S, V = np.linalg.svd(A)
    r = np.linalg.matrix_rank(A)
    if r == dim - 1:
        if np.linalg.det(U) * np.linalg.det(V) > 0:
            T[:dim, :dim] = U @ V
        else:
            s = d[dim - 1]; d[dim - 1] = -1; T[:dim, :dim] = U @ np.diag(d) @ V; d[dim - 1] = s
    else:
        T[:dim, :dim] = U @ np.diag(d) @ V
    scale = 1.0 / sd.var(0).sum() * (S @ d) if estimate_scale else 1.0
    T[:dim, dim] = dm - scale * (T[:dim, :dim] @ sm)
    T[:dim, :dim] *= scale
    return T


def whole_face_mat(lm68, S, pad=0.40, fore=0.07):
    """返回 image→aligned 的 2x3 仿射矩阵（= DFLJPG image_to_face_mat）。"""
    sub = np.concatenate([lm68[17:49], lm68[54:55]]).astype(np.float32)
    mat = umeyama(sub, _L2D_NEW, True)[0:2].astype(np.float32)
    def _t(pts):
        p = np.expand_dims(np.asarray(pts, dtype=np.float32), 1)
        return np.squeeze(cv2.transform(p, cv2.invertAffineTransform(mat), p.shape))
    g = _t([(0,0),(1,0),(1,1),(0,1),(0.5,0.5)]); gc = g[4].astype(np.float32)
    tb = (g[2]-g[0]).astype(np.float32); tb /= (np.linalg.norm(tb)+1e-8)
    bt = (g[1]-g[3]).astype(np.float32); bt /= (np.linalg.norm(bt)+1e-8)
    mod = np.linalg.norm(g[0]-g[2]) * (pad*np.sqrt(2.0)+0.5)
    vec = (g[0]-g[3]).astype(np.float32); vl = np.linalg.norm(vec)+1e-8; vec /= vl; gc = gc + vec*vl*fore
    l_t = np.array([gc-tb*mod, gc+bt*mod, gc+tb*mod], dtype=np.float32)
    return cv2.getAffineTransform(l_t, np.float32([(0,0),(S,0),(S,S)]))


def transform_points(points, mat):
    p = np.expand_dims(np.asarray(points, dtype=np.float32), 1)
    return np.squeeze(cv2.transform(p, mat, p.shape))


# ── DFLJPG 写/读（在标准 JPG 的 APP15/0xEF 段注入 pickle dict，与 DFL DFLJPG.py 同格式）──
def _parse_jpg_chunks(data: bytes):
    chunks, i, n = [], 0, len(data)
    while i < n:
        m_l, m_h = struct.unpack("BB", data[i:i+2]); i += 2
        if m_l != 0xFF:
            raise ValueError("非法 JPG")
        size = None; cdata = None; exdata = None
        hi = m_h & 0xF0
        if hi == 0xD0:
            k = m_h & 0x0F
            if 0 <= k <= 7 or k in (0x8, 0x9): size = 0
            elif k == 0xA: size = None       # SOS
            elif k == 0xD: size = 2
        if size is None:
            size, = struct.unpack(">H", data[i:i+2]); size -= 2; i += 2
        if size and size > 0:
            cdata = data[i:i+size]; i += size
        if (m_h & 0xF0) == 0xD0 and (m_h & 0x0F) == 0xA:   # SOS → 扫到 EOI
            c = i
            while c < n and (data[c] != 0xFF or data[c+1] != 0xD9):
                c += 1
            exdata = data[i:c]; i = c
        chunks.append({"m_h": m_h, "data": cdata, "ex_data": exdata})
    return chunks


def dfljpg_dump(img_bgr, dfl_dict, q=95):
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, q])
    if not ok:
        raise RuntimeError("JPG 编码失败")
    chunks = _parse_jpg_chunks(buf.tobytes())
    chunks = [c for c in chunks if c["m_h"] != 0xEF]     # 去旧 APP15
    last_app = 0
    for idx, c in enumerate(chunks):
        if c["m_h"] & 0xF0 == 0xE0:
            last_app = idx
    chunks.insert(last_app + 1, {"m_h": 0xEF, "data": pickle.dumps(dfl_dict), "ex_data": None})
    out = b""
    for c in chunks:
        out += struct.pack("BB", 0xFF, c["m_h"])
        if c["data"] is not None:
            out += struct.pack(">H", len(c["data"]) + 2) + c["data"]
        if c["ex_data"] is not None:
            out += c["ex_data"]
    return out


def dfljpg_read_dict(path):
    with open(path, "rb") as f:
        data = f.read()
    for c in _parse_jpg_chunks(data):
        if c["m_h"] == 0xEF and isinstance(c["data"], bytes):
            return pickle.loads(c["data"])
    return None


def build_analyser(gpu):
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


def run(args):
    t0 = time.time()
    fa = build_analyser(gpu=not args.cpu)
    keep = Path(args.keep)
    files = [p for p in sorted(keep.rglob("*")) if p.suffix.lower() in IMAGE_EXT]
    if not files:
        print(f"[!] {keep} 下无图片", file=sys.stderr); sys.exit(2)

    ref = None
    if not args.no_id_filter:
        # 用前若干张自举一个参考身份，过滤混入的他人脸
        seed = []
        for p in files[:min(30, len(files))]:
            im = imread_u(p); f = main_face(fa, im) if im is not None else None
            if f is not None:
                seed.append(f.normed_embedding)
        if seed:
            ref = np.mean(np.stack(seed), axis=0); ref /= (np.linalg.norm(ref) + 1e-8)

    out_dir = keep.parent / "aligned"
    out_dir.mkdir(parents=True, exist_ok=True)
    S = args.size
    stats = {"total": 0, "no_face": 0, "not_person": 0, "small": 0, "kept": 0}
    yaw_bins = {"<-25": 0, "-25..-10": 0, "-10..10": 0, "10..25": 0, ">25": 0}
    n = 0
    for p in files:
        stats["total"] += 1
        img = imread_u(p)
        if img is None:
            stats["no_face"] += 1; continue
        f = main_face(fa, img)
        if f is None:
            stats["no_face"] += 1; continue
        if ref is not None and float(np.dot(f.normed_embedding, ref)) < args.id_thresh:
            stats["not_person"] += 1; continue
        w, h = f.bbox[2] - f.bbox[0], f.bbox[3] - f.bbox[1]
        if max(w, h) < args.min_face:
            stats["small"] += 1; continue
        lm68 = np.asarray(f.landmark_3d_68[:, :2], dtype=np.float32)
        mat = whole_face_mat(lm68, S)
        aligned = cv2.warpAffine(img, mat, (S, S), flags=cv2.INTER_CUBIC)
        aligned_lm = transform_points(lm68, mat)
        n += 1
        dfl = {
            "face_type": "whole_face",
            "landmarks": aligned_lm.astype(np.float32).tolist(),
            "source_filename": p.name,
            "source_rect": [int(f.bbox[0]), int(f.bbox[1]), int(f.bbox[2]), int(f.bbox[3])],
            "source_landmarks": lm68.astype(np.float32).tolist(),
            "image_to_face_mat": mat.astype(np.float32).tolist(),
            "eyebrows_expand_mod": 1.0,
        }
        outp = out_dir / f"{n:06d}.jpg"
        with open(outp, "wb") as fh:
            fh.write(dfljpg_dump(aligned, dfl, q=args.quality))
        stats["kept"] += 1
        yaw = float(f.pose[1]) if getattr(f, "pose", None) is not None else 0.0
        if yaw < -25: yaw_bins["<-25"] += 1
        elif yaw < -10: yaw_bins["-25..-10"] += 1
        elif yaw <= 10: yaw_bins["-10..10"] += 1
        elif yaw <= 25: yaw_bins["10..25"] += 1
        else: yaw_bins[">25"] += 1
        if stats["kept"] % 500 == 0:
            print(f"  … 已对齐 {stats['kept']}")

    verdict = []
    k = stats["kept"]
    verdict.append(f"对齐脸 {k} 张 @ {S}px（whole_face, DFLJPG 内嵌元数据）")
    if k and (yaw_bins['<-25'] + yaw_bins['>25']) / k < 0.10:
        verdict.append("侧脸<10%：转头会露馅，建议补多角度视频")
    report = {"char": args.char, "size": S, "elapsed_s": round(time.time() - t0, 1),
              "stats": stats, "yaw_bins": yaw_bins, "verdict": verdict,
              "next": "把 aligned/ 作为 DeepFaceLab data_src/aligned；dst 用 RTM WF Faceset；跑 dfm_train.py"}
    (out_dir / "_faceset_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[✓] 对齐脸集 → {out_dir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DFM 训练素材：人脸提取/对齐 → DFL 可用 aligned 脸集")
    ap.add_argument("--char", required=True)
    ap.add_argument("--keep", required=True, help="清洗后的全帧目录（dfm_material 的 keep/）")
    ap.add_argument("--size", type=int, default=512, help="对齐输出边长（WF 常用 512）")
    ap.add_argument("--quality", type=int, default=95)
    ap.add_argument("--min-face", type=int, default=200, dest="min_face")
    ap.add_argument("--id-thresh", type=float, default=0.35, dest="id_thresh")
    ap.add_argument("--no-id-filter", action="store_true", dest="no_id_filter")
    ap.add_argument("--cpu", action="store_true")
    run(ap.parse_args())
