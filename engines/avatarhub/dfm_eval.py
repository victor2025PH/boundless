# -*- coding: utf-8 -*-
"""
DFM 辨识度评估（决定"练到什么程度可以上线"）
================================================
对一个训好的 .dfm：喂一批"目标脸(dst)"→ 得到换出的角色脸 → 用 ArcFace 量它到底
像不像该角色。核心指标(全部基于独立参考照片，不看训练集自评)：

  id_to_celeb : 换出脸 vs 角色真实参考照片 的平均余弦（越高越像该角色）
  id_to_dst   : 换出脸 vs 目标本人 的平均余弦（越低越好——说明换掉了原人身份）
  margin      : id_to_celeb - id_to_dst（>0 且越大＝辨识度越强，是最关键单一指标）
  flip_rate   : 换出脸"更像角色而非本人"的比例（1.0=全部成功换脸）
  det_rate    : 换出脸能被人脸检测器检出的比例（成形度/可用度）

阈值参考（经验）：margin>0.15 且 flip_rate>0.8 → 可上线做人肉 A/B；
                 margin<0.05 → 还没练出身份，继续训 / 补素材 / 升分辨率。

用法：
  python dfm_eval.py --model 刘德华.dfm --dst <目标脸集> --ref <角色真实参考照片夹> [--baseline-inswapper <源脸>]
"""
import sys, argparse, json
from pathlib import Path
import numpy as np, cv2

BASE = Path(r"C:\模仿音色")


def _cos(a, b):
    a = a / (np.linalg.norm(a) + 1e-8); b = b / (np.linalg.norm(b) + 1e-8)
    return float(np.dot(a, b))


def _imread(p):
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def _load_dfm_session(model):
    import onnxruntime as ort
    prov = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    try:
        s = ort.InferenceSession(str(model), providers=prov)
    except Exception:
        s = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    W = int(s.get_inputs()[0].shape[2])
    onames = [o.name for o in s.get_outputs()]
    return s, W, onames


def _mean_embed(fa, imgs):
    """一批图 → 各自最大脸的 ArcFace 归一化 embedding 的均值（+每张的列表）。"""
    embs = []
    for im in imgs:
        fs = fa.get(im)
        if not fs:
            continue
        f = max(fs, key=lambda x: (x.bbox[2]-x.bbox[0])*(x.bbox[3]-x.bbox[1]))
        embs.append(f.normed_embedding)
    if not embs:
        return None, []
    return np.mean(embs, axis=0), embs


def main():
    ap = argparse.ArgumentParser(description="DFM 辨识度评估")
    ap.add_argument("--model", required=True, help="训好的 .dfm")
    ap.add_argument("--dst", required=True, help="目标脸集目录（喂给模型换脸的输入，WF对齐脸或整图皆可）")
    ap.add_argument("--ref", required=True, help="角色真实参考照片目录（独立于训练集，判'像不像')")
    ap.add_argument("--n", type=int, default=60, help="从 dst 抽多少张评测")
    ap.add_argument("--out", default=None, help="评测对比图输出路径")
    a = ap.parse_args()

    from insightface.app import FaceAnalysis
    fa = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
    fa.prepare(ctx_id=0, det_size=(640, 640))

    sess, W, onames = _load_dfm_session(a.model)
    print(f"[eval] 模型={Path(a.model).name} input={W}x{W}")

    # 参考角色 & 目标本人的身份基准
    ref_imgs = [_imread(p) for p in sorted(Path(a.ref).glob("*")) if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    ref_emb, _ = _mean_embed(fa, ref_imgs[:80])
    if ref_emb is None:
        print("[!] 参考照片里没检出脸", file=sys.stderr); return 2

    dst_files = [p for p in sorted(Path(a.dst).glob("*")) if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    step = max(1, len(dst_files) // a.n)
    dst_files = dst_files[::step][:a.n]
    dst_imgs = [_imread(p) for p in dst_files]
    dst_emb, _ = _mean_embed(fa, dst_imgs)   # 目标本人身份基准

    to_celeb, to_dst, dets = [], [], 0
    rows = []
    for im in dst_imgs:
        face = cv2.resize(im, (W, W))
        blob = (face.astype(np.float32) / 255.0)[None]
        outs = sess.run(None, {"in_face:0": blob})
        omap = {n: o[0] for n, o in zip(onames, outs)}
        celeb = (np.clip(omap["out_celeb_face:0"], 0, 1) * 255).astype(np.uint8)
        # 换出脸补边后送检测器取 embedding
        pad = cv2.copyMakeBorder(celeb, W//3, W//3, W//3, W//3, cv2.BORDER_REFLECT)
        fs = fa.get(pad)
        if fs:
            dets += 1
            e = max(fs, key=lambda x: (x.bbox[2]-x.bbox[0])*(x.bbox[3]-x.bbox[1])).normed_embedding
            to_celeb.append(_cos(e, ref_emb))
            if dst_emb is not None:
                to_dst.append(_cos(e, dst_emb))
        if len(rows) < 5:
            rows.append(np.hstack([face, celeb]))

    n = len(dst_imgs)
    mc = float(np.mean(to_celeb)) if to_celeb else 0.0
    md = float(np.mean(to_dst)) if to_dst else 0.0
    margin = mc - md
    flip = float(np.mean([c > d for c, d in zip(to_celeb, to_dst)])) if (to_celeb and to_dst) else 0.0
    det_rate = dets / max(1, n)

    verdict = ("可上线做人肉A/B" if (margin > 0.15 and flip > 0.8) else
               "接近，建议再训/补素材" if margin > 0.05 else
               "身份尚未成形，需继续训练或升分辨率/补素材")
    report = {"model": Path(a.model).name, "input": W, "n_eval": n,
              "id_to_celeb": round(mc, 4), "id_to_dst": round(md, 4),
              "margin": round(margin, 4), "flip_rate": round(flip, 3),
              "det_rate": round(det_rate, 3), "verdict": verdict}
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if rows:
        out = a.out or str(Path(a.model).with_suffix("").as_posix() + "_eval.jpg")
        cv2.imencode(".jpg", np.vstack(rows))[1].tofile(out)
        print(f"[eval] 对比图(输入|换出) → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
