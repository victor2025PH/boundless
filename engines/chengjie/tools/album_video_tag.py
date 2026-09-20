# -*- coding: utf-8 -*-
"""相册视频打标：抽帧 → VLM 场景/触发词 → 更新 manifest + persona_media.db。

补 album_curator 的缺口（视频入库时只有"视频"泛触发词）：
每个视频抽中段 1 帧交 VLM 分析，产出 scene/triggers/caption/description，
系列标签 series:video-<scene>（同场景视频互为系列，防复读排除用）。

用法：
    python tools/album_video_tag.py --manifest <album>/manifest.json \
        --db <config>/persona_media.db [--vlm http://192.168.0.176:11434/v1]
幂等：manifest 里已有 scene 的视频跳过（重跑安全）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.album_curator import vlm_analyze  # noqa: E402  复用图片分析（含重试/清洗）


def extract_mid_frame(video: Path, out_jpg: Path) -> bool:
    """ffmpeg 取视频中段一帧（50% 时长处）；失败返回 False。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=30)
        dur = float((r.stdout or "0").strip() or 0)
    except Exception:
        dur = 0.0
    ts = max(0.5, dur * 0.5)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{ts:.2f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "3", str(out_jpg)],
            capture_output=True, timeout=60, check=True)
        return out_jpg.exists() and out_jpg.stat().st_size > 0
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--vlm", default="http://192.168.0.176:11434/v1")
    ap.add_argument("--model", default="qwen2.5vl:7b")
    args = ap.parse_args()

    mf_path = Path(args.manifest)
    mf = json.loads(mf_path.read_text(encoding="utf-8"))
    album_dir = mf_path.parent
    persona = str(mf.get("persona") or "")

    from src.companion.persona_media_store import PersonaMediaStore
    store = PersonaMediaStore(args.db)

    done = skip = fail = 0
    with tempfile.TemporaryDirectory() as td:
        for v in mf.get("videos", []):
            if v.get("scene"):
                skip += 1
                continue
            vp = album_dir / v["file"]
            frame = Path(td) / (vp.stem + ".jpg")
            if not extract_mid_frame(vp, frame):
                print(f"[video_tag] 抽帧失败: {v['file']}")
                fail += 1
                continue
            try:
                meta = vlm_analyze(args.vlm, args.model, frame)
            except Exception as e:  # noqa: BLE001
                print(f"[video_tag] VLM 失败 {v['file']}: {e}")
                fail += 1
                continue
            v["scene"] = meta["scene"]
            v["description"] = meta["description"]
            v["triggers"] = meta["triggers"]
            v["caption"] = meta["caption"]
            # DB：按 sha 定位行，更新 triggers/caption/tags（series 换成场景系列）
            row = store.find_by_sha(persona, str(v.get("sha256") or ""))
            if row:
                trg = list({*(meta["triggers"] or []), "视频"})
                tags = [t for t in (row.get("tags") or [])
                        if not str(t).startswith(("series:", "scene:"))]
                tags += [f"scene:{meta['scene']}", f"series:video-{meta['scene']}"]
                store.update(str(row["id"]), triggers=trg,
                             caption=meta["caption"], tags=tags)
            done += 1
            print(f"[video_tag] {v['file']} → scene={meta['scene']} "
                  f"triggers={meta['triggers']}")

    mf_path.write_text(json.dumps(mf, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    print(f"[video_tag] 完成 done={done} skip={skip} fail={fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
