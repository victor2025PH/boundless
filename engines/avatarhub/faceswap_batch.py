# -*- coding: utf-8 -*-
"""faceswap_batch.py — 批量把素材做成「竖版直播换脸演示」，可直接进每日发布队列。

它解决一个现实问题：stodownload 那种「大窗 + 右上角真人小窗」的双窗素材很稀缺。
本脚本能把**任意一段普通单人口播视频**自动排成同款竖版直播版式，再只换大窗的脸为
美女，右上小窗保留同一段画面的真人脸 —— 两窗天生逐帧同步（同一段素材驱动），
正好充当「换脸前的真人操作者」证据，效果与 stodownload 同理但素材随处可得。

两种模式（每条 job 选一）：
  compose：素材是普通单人视频 → 先合成竖版 720x1288 + 右上角真人小窗（同段缩小），
           再只换大窗脸为美女；小窗保留真人。→ 想批量造演示片用这个。
  swap   ：素材本身就是双窗视频（如 stodownload）→ 直接大窗换美女、角落换指定人脸、
           顺带去水印。→ 已有双窗素材用这个。

配置：publish/faceswap_jobs.json（数组），字段见 sample_jobs()。
调用底层 faceswap_video.py 做真正的换脸，本脚本只负责裁剪/合成/排队。

在 facefusion 环境跑：
  & C:/Users/user/Miniconda3/envs/facefusion/python.exe faceswap_batch.py
  可选 --only name1,name2 只跑指定 job；--no-queue 不进队列只出片到 faceswap_out/。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
JOBS_FILE = BASE / "publish" / "faceswap_jobs.json"
OUT_DIR = BASE / "publish" / "faceswap_out"
QUEUE_DIR = BASE / "publish" / "queue"
WORK_DIR = BASE / "publish" / "work_faceswap"
SWAP_SCRIPT = BASE / "faceswap_video.py"

# 竖版直播版式默认参数
CANVAS_W, CANVAS_H = 720, 1288
PIP_W, PIP_H = 250, 330      # 右上角真人小窗尺寸
PIP_MARGIN = 8


def find_ffmpeg() -> str:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    pkgs = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    hits = sorted(pkgs.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe")) if pkgs.exists() else []
    if hits:
        return str(hits[-1])
    raise SystemExit("[批处理] 找不到 ffmpeg")


def compose_vertical(ff: str, src: Path, dst: Path, seconds: int | None, corner: str):
    """把任意视频排成竖版 720x1288 + 一角真人小窗（同段画面缩小）。"""
    pos = {
        "tr": f"W-w-{PIP_MARGIN}:{PIP_MARGIN}",
        "tl": f"{PIP_MARGIN}:{PIP_MARGIN}",
        "br": f"W-w-{PIP_MARGIN}:H-h-{PIP_MARGIN}",
        "bl": f"{PIP_MARGIN}:H-h-{PIP_MARGIN}",
    }[corner]
    fc = (
        f"[0:v]scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,"
        f"crop={CANVAS_W}:{CANVAS_H},setsar=1[bg];"
        f"[0:v]scale={PIP_W}:{PIP_H}:force_original_aspect_ratio=increase,"
        f"crop={PIP_W}:{PIP_H},"
        f"pad=iw+6:ih+6:3:3:white,setsar=1[pip];"       # 小窗描白边，更像真实推流叠层
        f"[bg][pip]overlay={pos}[v]"
    )
    cmd = [ff, "-y", "-v", "error"]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-i", str(src), "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(dst)]
    subprocess.run(cmd, check=True)


def trim_only(ff: str, src: Path, dst: Path, seconds: int):
    subprocess.run(
        [ff, "-y", "-v", "error", "-t", str(seconds), "-i", str(src),
         "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(dst)],
        check=True)


def resolve(p: str) -> Path:
    """相对路径按项目根解析，支持中文/绝对路径。"""
    path = Path(p)
    return path if path.is_absolute() else (BASE / p)


def run_job(job: dict, ff: str, to_queue: bool) -> Path:
    name = job["name"]
    mode = job.get("mode", "compose")
    src = resolve(job["input"])
    if not src.exists():
        raise SystemExit(f"[批处理] 素材不存在：{src}")
    corner = job.get("corner", "tr")
    seconds = job.get("seconds")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # 第一步：准备喂给换脸的输入（compose=排版；swap=按需裁剪）
    prepped = WORK_DIR / f"{name}_prep.mp4"
    if mode == "compose":
        print(f"[批处理] {name}：合成竖版直播版式（{corner} 角真人小窗）…")
        compose_vertical(ff, src, prepped, seconds, corner)
    else:  # swap
        if seconds:
            trim_only(ff, src, prepped, seconds)
        else:
            prepped = src

    # 第二步：换脸（compose 只换大窗；swap 大窗+角落，并去水印）
    out = OUT_DIR / f"{name}.mp4"
    cmd = [sys.executable, str(SWAP_SCRIPT),
           "--input", str(prepped), "--output", str(out),
           "--main-face", str(resolve(job["main_face"])),
           "--corner", corner, "--det-size", str(job.get("det_size", 1280))]
    if job.get("no_enhance"):
        cmd.append("--no-enhance")
    if mode == "swap" and job.get("corner_face"):   # 角落换脸只在 swap 模式（compose 的小窗要留真人）
        cmd += ["--corner-face", str(resolve(job["corner_face"]))]
    if job.get("delogo"):                            # 去水印两种模式都支持（坐标按最终 720x1288 画布算）
        cmd += ["--delogo", job["delogo"]]
    print(f"[批处理] {name}：调用换脸引擎…")
    subprocess.run(cmd, check=True)

    # 第三步：进队列 + 写文案 sidecar
    if to_queue and job.get("queue", True):
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        q_mp4 = QUEUE_DIR / f"{name}.mp4"
        shutil.copy2(out, q_mp4)
        meta = {k: job[k] for k in ("title_zh", "title_en", "desc_zh", "desc_en")
                if k in job}
        meta.setdefault("ai", True)
        if meta:
            q_mp4.with_suffix(".json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[批处理] {name}：已进发布队列 → {q_mp4.name}")
    if prepped != src:
        prepped.unlink(missing_ok=True)
    return out


def sample_jobs() -> list[dict]:
    return [
        {
            "name": "demo-beauty-compose",
            "mode": "compose",
            "input": "C:/Users/user/Desktop/明星/换成你的口播素材.mp4",
            "main_face": "faces/默认.jpg",
            "corner": "tr",
            "seconds": 15,
            "title_zh": "实时换脸 · 素人秒变超级美女",
            "title_en": "Real-time face swap · anyone becomes a stunning beauty",
            "desc_zh": "右上小窗是换脸前的真人，大画面是换脸后的超级美女——转头、眨眼、说话逐帧一致，只有脸变了。概念演示。",
            "desc_en": "Corner window is the real face before the swap; main view is a stunning beauty — every move matches frame for frame, only the face changes. Concept demo.",
            "queue": True
        },
        {
            "name": "demo-sto-swap",
            "mode": "swap",
            "input": "C:/Users/user/Desktop/明星/stodownload.mp4",
            "main_face": "faces/默认.jpg",
            "corner_face": "faces/刘德华.jpg",
            "corner": "tr",
            "delogo": "6:1055:212:150",
            "title_zh": "直播实时换脸 · 双窗同步",
            "title_en": "Live face swap · dual-window in sync",
            "queue": True
        }
    ]


def main():
    ap = argparse.ArgumentParser(description="批量生成竖版直播换脸演示片")
    ap.add_argument("--only", default="", help="只跑这些 job（逗号分隔的 name）")
    ap.add_argument("--no-queue", action="store_true", help="只出片到 faceswap_out/，不进发布队列")
    ap.add_argument("--init", action="store_true", help="写出示例 faceswap_jobs.json 后退出")
    args = ap.parse_args()

    if args.init or not JOBS_FILE.exists():
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        JOBS_FILE.write_text(json.dumps(sample_jobs(), ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"[批处理] 已写示例配置：{JOBS_FILE}")
        if args.init:
            return

    jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        jobs = [j for j in jobs if j["name"] in want]
    if not jobs:
        print("[批处理] 没有可跑的 job。")
        return

    ff = find_ffmpeg()
    ok, fail = [], []
    for job in jobs:
        try:
            out = run_job(job, ff, not args.no_queue)
            ok.append(f"{job['name']} → {out}")
        except Exception as e:
            fail.append(f"{job['name']}：{e}")
            print(f"[批处理] {job['name']} 失败：{e}")
    print("\n[批处理] 完成。成功 {}，失败 {}".format(len(ok), len(fail)))
    for line in ok:
        print("  ✓ " + line)
    for line in fail:
        print("  ✗ " + line)


if __name__ == "__main__":
    main()
