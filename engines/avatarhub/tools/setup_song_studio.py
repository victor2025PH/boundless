# -*- coding: utf-8 -*-
"""
Song Studio（AI 翻唱）一键部署：依赖安装 + 权重下载 + 冒烟自检。

用法（任一 python 均可跑下载；--install-deps 需指定 ymsvc 环境）：
  python tools/setup_song_studio.py --download          # 只下载权重（断点续传，可反复跑）
  python tools/setup_song_studio.py --install-deps      # 只装 ymsvc 依赖（阿里云镜像）
  python tools/setup_song_studio.py --verify            # 只做文件完整性核对
  python tools/setup_song_studio.py --all               # 全部

本机约束（升级路线图 2026-06-01 已验证）：
- 直连 HF ~50KB/s；hf-mirror /resolve/ 重定向对象支持 Range 且逐连接限速
  → 16 线程分段并行 + 断点续传，实测可到 ~5MB/s。
- PyPI 用阿里云镜像（~1MB/s）。
"""
import argparse
import os
import subprocess
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(BASE, "models", "song_studio")
YMSVC_PY = os.environ.get(
    "YMSVC_PY",
    os.path.join(os.path.expanduser("~"), "miniconda3", "envs", "ymsvc", "python.exe"))

HOSTS = ["https://hf-mirror.com", "https://huggingface.co"]
N_THREADS = 16

# (repo, filename_in_repo, dest_relpath, expected_size_or_None)
MANIFEST = [
    ("GiantAILab/YingMusic-SVC", "YingMusic-SVC-full.pt",
     "YingMusic-SVC-full.pt", 731683240),
    ("GiantAILab/YingMusic-SVC", "bs_roformer.ckpt",
     "bs_roformer.ckpt", 1295074954),
    # Song-P3/O2: Mel-Band RoFormer 人声分离（Kim 社区权重，MIT）——「精细档」分离模型。
    # 模型代码 accom_separation 内置（models/bs_roformer/mel_band_roformer.py），只补权重即可。
    ("KimberleyJSN/melbandroformer", "MelBandRoformer.ckpt",
     "mel_band_roformer.ckpt", 913106900),
    ("nvidia/bigvgan_v2_44khz_128band_512x", "config.json",
     os.path.join("bigvgan_v2_44khz_128band_512x", "config.json"), 1403),
    ("nvidia/bigvgan_v2_44khz_128band_512x", "bigvgan_generator.pt",
     os.path.join("bigvgan_v2_44khz_128band_512x", "bigvgan_generator.pt"), 489041291),
    ("openai/whisper-small", "config.json",
     os.path.join("whisper-small", "config.json"), None),
    ("openai/whisper-small", "preprocessor_config.json",
     os.path.join("whisper-small", "preprocessor_config.json"), None),
    ("openai/whisper-small", "model.safetensors",
     os.path.join("whisper-small", "model.safetensors"), None),
    ("funasr/campplus", "campplus_cn_common.bin",
     "campplus_cn_common.bin", 28036335),
]

# rmvpe.pt 本地已有（RVC 资产），直接复制，省 173MB 下载
RMVPE_LOCAL = os.path.join(BASE, "Retrieval-based-Voice-Conversion-WebUI",
                           "assets", "rmvpe", "rmvpe.pt")

# ymsvc 推理最小依赖集（静态扫描 my_inference/mm4/modules/accom_separation 得出；
# torch 2.11.0+cu128 已在环境中，勿动 —— 2.4.x 不支持 5090 sm_120）
PIP_PKGS = [
    "numpy==1.26.4", "scipy", "librosa==0.10.2", "soundfile",
    "pyyaml", "munch", "einops", "tqdm", "requests",
    "transformers==4.46.3", "huggingface_hub",
    "descript-audio-codec",          # modules/length_regulator 顶层 import dac
    "matplotlib",                    # modules/flow_matching 顶层 import pyplot
    "omegaconf", "ml_collections", "loralib", "wandb",   # accom_separation.utils.settings 顶层 import
    "beartype==0.14.1", "rotary_embedding_torch==0.3.5",  # bs_roformer
    "fastapi", "uvicorn", "python-multipart",             # song_studio_server
    "imageio-ffmpeg",                # mp3/m4a 解码兜底（暴露 ffmpeg.exe）
]
PIP_MIRROR = "https://mirrors.aliyun.com/pypi/simple/"


def _probe_size(url: str) -> int:
    import requests
    r = requests.get(url, headers={"Range": "bytes=0-0"}, stream=True,
                     timeout=30, allow_redirects=True)
    r.raise_for_status()
    cr = r.headers.get("Content-Range", "")
    if "/" in cr:
        return int(cr.split("/")[-1])
    return int(r.headers.get("Content-Length", "0"))


def _parallel_fetch(url: str, out_path: str, total: int, n: int = N_THREADS) -> bool:
    """分段并行下载（断点续传）。小文件(<8MB)单线程。"""
    import requests
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path) and os.path.getsize(out_path) == total:
        print(f"  [skip] 已存在且大小一致: {os.path.basename(out_path)}")
        return True
    if total < 8 * 1024 * 1024:
        for attempt in range(5):
            try:
                r = requests.get(url, timeout=60, allow_redirects=True)
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(r.content)
                return os.path.getsize(out_path) == total
            except Exception as e:
                print(f"  [retry {attempt+1}] {e}")
                time.sleep(3)
        return False

    part_dir = out_path + ".parts"
    os.makedirs(part_dir, exist_ok=True)
    seg = total // n
    ranges = [(i, i * seg, (total - 1) if i == n - 1 else (i + 1) * seg - 1)
              for i in range(n)]
    ok_flags = [False] * n

    def worker(i, start, end):
        part = os.path.join(part_dir, f"part_{i:02d}")
        target = end - start + 1
        for _ in range(200):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have >= target:
                ok_flags[i] = True
                return
            try:
                headers = {"Range": f"bytes={start + have}-{end}"}
                with requests.get(url, headers=headers, stream=True,
                                  timeout=60, allow_redirects=True) as r:
                    r.raise_for_status()
                    with open(part, "ab") as f:
                        for chunk in r.iter_content(256 * 1024):
                            if chunk:
                                f.write(chunk)
            except Exception:
                time.sleep(3)
        ok_flags[i] = False

    threads = [threading.Thread(target=worker, args=rg, daemon=True) for rg in ranges]
    for t in threads:
        t.start()
    t0 = time.time()
    while any(t.is_alive() for t in threads):
        time.sleep(10)
        got = sum(os.path.getsize(os.path.join(part_dir, f"part_{i:02d}"))
                  if os.path.exists(os.path.join(part_dir, f"part_{i:02d}")) else 0
                  for i in range(n))
        el = max(1, time.time() - t0)
        print(f"  [{int(el)}s] {got/1e6:.0f}/{total/1e6:.0f} MB "
              f"({got/1e6/el:.2f} MB/s)", flush=True)
    for t in threads:
        t.join()
    if not all(ok_flags):
        return False
    with open(out_path, "wb") as out:
        for i in range(n):
            with open(os.path.join(part_dir, f"part_{i:02d}"), "rb") as f:
                while True:
                    b = f.read(8 * 1024 * 1024)
                    if not b:
                        break
                    out.write(b)
    if os.path.getsize(out_path) != total:
        print(f"  [FAIL] 合并后大小不一致: {os.path.getsize(out_path)} != {total}")
        return False
    import shutil
    shutil.rmtree(part_dir, ignore_errors=True)
    return True


def do_download() -> bool:
    all_ok = True
    for repo, fname, rel, size in MANIFEST:
        dest = os.path.join(MODELS_DIR, rel)
        print(f"[fetch] {repo}/{fname} → {rel}")
        done = False
        for host in HOSTS:
            url = f"{host}/{repo}/resolve/main/{fname}"
            try:
                total = size or _probe_size(url)
            except Exception as e:
                print(f"  [probe fail] {host}: {e}")
                continue
            if _parallel_fetch(url, dest, total):
                done = True
                break
            print(f"  [host fail] {host}")
        if not done:
            print(f"  [FAIL] {fname}")
            all_ok = False
    # rmvpe 本地复制
    rmvpe_dest = os.path.join(MODELS_DIR, "rmvpe.pt")
    if not os.path.exists(rmvpe_dest):
        if os.path.exists(RMVPE_LOCAL):
            import shutil
            print(f"[copy] rmvpe.pt ← RVC assets（本地复用，省 173MB 下载）")
            shutil.copyfile(RMVPE_LOCAL, rmvpe_dest)
        else:
            print("[fetch] rmvpe.pt（本地 RVC 资产缺失，改走下载）")
            done = False
            for host in HOSTS:
                url = f"{host}/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt"
                try:
                    total = _probe_size(url)
                    if _parallel_fetch(url, rmvpe_dest, total):
                        done = True
                        break
                except Exception as e:
                    print(f"  [probe fail] {host}: {e}")
            all_ok = all_ok and done
    return all_ok


def do_install_deps() -> bool:
    if not os.path.exists(YMSVC_PY):
        print(f"[FAIL] ymsvc python 不存在: {YMSVC_PY}")
        return False
    cmd = [YMSVC_PY, "-m", "pip", "install", "-i", PIP_MIRROR,
           "--timeout", "120"] + PIP_PKGS
    print("[pip]", " ".join(cmd))
    return subprocess.call(cmd) == 0


def do_verify() -> bool:
    ok = True
    for _, _, rel, size in MANIFEST:
        p = os.path.join(MODELS_DIR, rel)
        if not os.path.exists(p):
            print(f"[MISS] {rel}")
            ok = False
        elif size and os.path.getsize(p) != size:
            print(f"[SIZE] {rel}: {os.path.getsize(p)} != {size}")
            ok = False
        else:
            print(f"[OK]   {rel}  {os.path.getsize(p)/1e6:.1f} MB")
    p = os.path.join(MODELS_DIR, "rmvpe.pt")
    print(f"[{'OK' if os.path.exists(p) else 'MISS'}]   rmvpe.pt")
    ok = ok and os.path.exists(p)
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--install-deps", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    rc = 0
    if args.all or args.install_deps:
        rc |= 0 if do_install_deps() else 1
    if args.all or args.download:
        rc |= 0 if do_download() else 2
    if args.all or args.verify or args.download:
        rc |= 0 if do_verify() else 4
    print("RESULT:", "OK" if rc == 0 else f"FAIL({rc})")
    sys.exit(rc)
