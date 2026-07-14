# -*- coding: utf-8 -*-
"""素材种子包导入（2026-07-08 阶段6·P0）：VITON-HD-TEST → 服装库 + 发型参考库。

一份下载喂两个库：
  · cloth/  100 件白底上衣平铺图 → clothes\\演示上衣NNN.jpg（FitDiT 同源训练数据，效果最稳）
  · image/  100 张高清正面模特照 → 挑 30 张 → hair_styles\\演示发型NNN.jpg
    （HairFastGAN 只要"带目标发型的人像"，模特照天然合格）

发型质检（深入思考的优化点）：不均匀瞎采样，用 MediaPipe FaceLandmarker
（models\\face_landmarker.task，妆容服务同款 3.7MB 模型）过滤——检不出脸的
（侧脸/遮挡/背影）直接淘汰，按人脸框面积排序取前 30（脸大=对齐成功率高）。
mediapipe 缺失时自动退化为均匀采样，不阻断导入。

命名带「演示」前缀：与用户自家商品图区分——VITON-HD 仅限研究用途，
直播带货请用「截图抠衣」导自家商品。

幂等：同名已存在跳过（--force 覆盖）。用法:
  python tools/asset_import.py            # 全量导入（VITON-HD 上装+发型）
  python tools/asset_import.py --clothes-only / --hair-only / --force
  python tools/asset_import.py --dresscode # 阶段7：下装25+连衣裙25（DressCode-Test）
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(r"c:\模仿音色")
DATASET_DIR = Path(r"C:\datasets\viton_hd_test")
CLOTH_DIR = BASE / "clothes"
HAIR_DIR = BASE / "hair_styles"
REPO = "TryOnVirtual/VITON-HD-TEST"
N_HAIR = 30


def download() -> Path:
    from huggingface_hub import snapshot_download
    print(f"[dl] {REPO} (cloth/ + image/) → {DATASET_DIR}")
    p = snapshot_download(REPO, repo_type="dataset", local_dir=str(DATASET_DIR),
                          allow_patterns=["cloth/*", "image/*"])
    root = Path(p)
    n_c = len(list((root / "cloth").glob("*.jpg")))
    n_i = len(list((root / "image").glob("*.jpg")))
    print(f"[dl] 完成 cloth={n_c} image={n_i}")
    return root


def import_clothes(root: Path, force: bool) -> int:
    CLOTH_DIR.mkdir(exist_ok=True)
    n = 0
    for i, f in enumerate(sorted((root / "cloth").glob("*.jpg")), 1):
        dst = CLOTH_DIR / f"演示上衣{i:03d}.jpg"
        if dst.exists() and not force:
            continue
        shutil.copyfile(f, dst)
        n += 1
    print(f"[clothes] 新入库 {n} 件（库内共 {len(list(CLOTH_DIR.glob('*.jpg')))} 件）")
    return n


def _face_scores(files: list) -> list:
    """MediaPipe 过滤：返回 [(score, path)]，score=人脸框面积占比；检不出=淘汰。"""
    import mediapipe as mp
    from mediapipe.tasks.python import vision, BaseOptions
    import numpy as np
    import cv2
    task = (BASE / "models" / "face_landmarker.task").read_bytes()   # 中文路径→buffer 绕行
    lm = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_buffer=task), num_faces=1))
    out = []
    for f in files:
        img = cv2.imdecode(np.frombuffer(f.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                 data=cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
        if not res.face_landmarks:
            continue
        xs = [p.x for p in res.face_landmarks[0]]
        ys = [p.y for p in res.face_landmarks[0]]
        out.append(((max(xs) - min(xs)) * (max(ys) - min(ys)), f))
    lm.close()
    return sorted(out, reverse=True)


def import_hair(root: Path, force: bool) -> int:
    HAIR_DIR.mkdir(exist_ok=True)
    files = sorted((root / "image").glob("*.jpg"))
    try:
        scored = _face_scores(files)
        picked = [f for _, f in scored[:N_HAIR]]
        print(f"[hair] MediaPipe 质检：{len(files)} 张中 {len(scored)} 张检出人脸，"
              f"按脸部占比取前 {len(picked)}")
    except Exception as e:
        step = max(1, len(files) // N_HAIR)
        picked = files[::step][:N_HAIR]
        print(f"[hair] 质检不可用({str(e)[:60]})，退化为均匀采样 {len(picked)} 张")
    n = 0
    for i, f in enumerate(picked, 1):
        dst = HAIR_DIR / f"演示发型{i:03d}.jpg"
        if dst.exists() and not force:
            continue
        shutil.copyfile(f, dst)
        n += 1
    print(f"[hair] 新入库 {n} 张（库内共 {len(list(HAIR_DIR.glob('*.jpg')))} 张）")
    return n


# ── DressCode-Test（阶段7）：下装/连衣裙素材 ────────────────────────────
DRESSCODE_TAR = Path(r"C:\datasets\dresscode_test\DressCode-Test.tar.gz")
N_PER_CAT = 25


def import_dresscode(force: bool) -> int:
    """从 DressCode-Test.tar.gz 抽 lower/dresses 平铺图入库（upper 已有 VITON 100 件）。
    类别真值来自 test_pairs_unpaired.txt 第三列（person_0 cloth_1 category），
    无需下载原版 DressCode（要 Google 表单申请）。"""
    import tarfile
    if not DRESSCODE_TAR.exists():
        from huggingface_hub import hf_hub_download
        print("[dresscode] 下载 DressCode-Test.tar.gz (1.26GB)…")
        hf_hub_download("zhengchong/DressCode-Test", "DressCode-Test.tar.gz",
                        repo_type="dataset", local_dir=str(DRESSCODE_TAR.parent))
    CLOTH_DIR.mkdir(exist_ok=True)
    t = tarfile.open(DRESSCODE_TAR, "r:gz")
    pairs = t.extractfile("DressCode-Test/test_pairs_unpaired.txt").read().decode()
    cat_of: dict = {}                                  # cloth 文件名 → 类别
    for ln in pairs.strip().splitlines():
        parts = ln.split()
        if len(parts) >= 3:
            cat_of[parts[1]] = parts[2].lower()
    label = {"lower": "演示裤装", "dresses": "演示连衣裙", "dress": "演示连衣裙"}
    counter: dict = {}
    n = 0
    for fname, cat in sorted(cat_of.items()):
        zh = label.get(cat)
        if not zh:
            continue
        i = counter.get(zh, 0)
        if i >= N_PER_CAT:
            continue
        dst = CLOTH_DIR / f"{zh}{i + 1:03d}.jpg"
        counter[zh] = i + 1
        if dst.exists() and not force:
            continue
        m = t.extractfile(f"DressCode-Test/cloth/{fname}")
        if m is None:
            continue
        dst.write_bytes(m.read())
        n += 1
    t.close()
    print(f"[dresscode] 新入库 {n} 件 {dict(counter)}（库内共 "
          f"{len(list(CLOTH_DIR.glob('*.jpg')))} 件）")
    return n


def notify_services():
    """服装库让 8002 重载（GET /clothes 自带 reload）；发型库 8001 启动时自读。"""
    try:
        import requests
        r = requests.get("http://127.0.0.1:8002/clothes", timeout=8)
        print(f"[reload] 8002 服装库现挂 {len(r.json().get('clothes', []))} 件")
    except Exception as e:
        print(f"[reload] 8002 不在线（下次启动自动加载）: {str(e)[:60]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clothes-only", action="store_true")
    ap.add_argument("--hair-only", action="store_true")
    ap.add_argument("--dresscode", action="store_true", help="只导 DressCode 下装/连衣裙")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.dresscode:
        import_dresscode(a.force)
    else:
        root = download()
        if not a.hair_only:
            import_clothes(root, a.force)
        if not a.clothes_only:
            import_hair(root, a.force)
    notify_services()
    print("[done] 授权提醒：演示素材仅限研究/演示；带货请用「截图抠衣」导自家商品图")


if __name__ == "__main__":
    main()
