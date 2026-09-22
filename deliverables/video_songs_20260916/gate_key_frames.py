#!/usr/bin/env python3
"""关键集抽帧目检闸：按 clip 边界抽点（避开切段空隙）+ PNG magic。

  python gate_key_frames.py
  python gate_key_frames.py --only E3,E4,E5,E9,E7,E8,E12
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
INTRO_S = 8.0
END_S = 7.0

RAP_TAKE = {
    "E1": 426, "E2": 428, "E3": 429, "E4": 430, "E5": 431,
    "E6": 432, "E7": 433, "E8": 434, "E9": 435, "E10": 436, "E11": 437,
    "E12": 452,
}

EXPECT = {
    "E3": [
        "翻译开关或译文字幕可见（#xlate-toggle / 译气泡）",
        "BOUNDLESS 演示会话头栏（仅本集允许）",
        "大红圈指针指向翻译相关控件",
    ],
    "E4": [
        "模式选择 #mode-select 或模式菜单",
        "AI 回复主按钮 #ai-reply-btn（不是「生成草稿」文案）",
        "回复台/草稿区在业务助手侧",
    ],
    "E5": [
        "/personas 人设列表或人设卡",
        "expand_card persona 后的绑定/切换控件",
    ],
    "E9": [
        "/care-schedule 或 /episodic-memory 页面标题/主控件",
        "关怀时段或记忆条目可见",
    ],
    "E7": ["工具箱语音卡展开后可见「生成语音」"],
    "E8": ["客户关系工作目标卡展开后可见「设定目标」"],
    "E12": ["会员中心 #mb-hero / 购买续费，禁止空 pricing"],
}


def ffprobe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:200])
    return json.loads(r.stdout)


def extract(mp4: Path, t: float, dst: Path) -> bool:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1", str(dst)],
        capture_output=True,
    )
    if not dst.exists() or dst.stat().st_size < 20_000:
        return False
    return dst.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def clip_times(dur: float, n_clips: int) -> list[tuple[str, float]]:
    """在说唱段内按 clip 数均分，取每段 55% 处——避开段落切换黑场。"""
    n = max(1, n_clips)
    rap = max(6.0, dur - INTRO_S - END_S)
    out: list[tuple[str, float]] = []
    for i in range(n):
        t = INTRO_S + (i + 0.55) * (rap / n)
        t = min(max(INTRO_S + 1.0, t), dur - 2.0)
        out.append((f"c{i}", t))
    # 另补说唱中点（总览）
    mid = INTRO_S + rap * 0.5
    out.append(("mid", min(mid, dur - 2.0)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="E3,E4,E5,E9")
    a = ap.parse_args()
    want = [x.strip() for x in a.only.split(",") if x.strip()]
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    ep_clips = {
        e["id"]: [c.get("clip") or f"c{i}" for i, c in enumerate(e.get("footage_actions") or [])]
        for e in cur.get("episodes") or []
    }

    fails: list[str] = []
    checklist: list[str] = []
    for eid in want:
        take = RAP_TAKE.get(eid)
        print(f"-- {eid}")
        mp4 = OUT / eid / f"{eid}_h{take}_episode.mp4" if take else None
        if mp4 is None or not mp4.exists():
            alt = sorted((OUT / eid).glob(f"{eid}_h*_episode.mp4")) if (OUT / eid).exists() else []
            if alt:
                mp4 = alt[-1]
                print(f"  ~ 用现成成片 {mp4.name}")
            else:
                fails.append(f"{eid}: 缺成片")
                print("  FAIL missing mp4")
                continue
        try:
            info = ffprobe(mp4)
        except Exception as e:
            fails.append(f"{eid}: ffprobe {e}")
            continue
        kinds = {s["codec_type"] for s in info["streams"]}
        dur = float(info["format"]["duration"])
        if kinds != {"video", "audio"}:
            fails.append(f"{eid}: streams={kinds}")
        clips = ep_clips.get(eid) or ["a"]
        times = clip_times(dur, len(clips))
        ok_n = 0
        for tag, t in times:
            # 文件名带 clip 名更可读
            clip_name = clips[int(tag[1:])] if tag.startswith("c") and tag[1:].isdigit() and int(tag[1:]) < len(clips) else tag
            dst = OUT / eid / f"keyframe_{clip_name}_t{int(t)}.png"
            ok = extract(mp4, t, dst)
            print(f"  frame {clip_name}@{t:.1f}s → {'OK' if ok else 'FAIL'} {dst.name}")
            if ok:
                ok_n += 1
                checklist.append(f"{eid}/{dst.name}")
            else:
                fails.append(f"{eid}: frame {clip_name} fail")
        print(f"  mp4 {dur:.1f}s streams={kinds} clips={len(clips)} frames={ok_n}/{len(times)}")
        print("  EXPECT:")
        for line in EXPECT.get(eid, []):
            print(f"    - {line}")

    print("== gate_key_frames ==")
    print("CHECKLIST frames:", len(checklist))
    for p in checklist:
        print(" ", p)
    for f in fails:
        print("FAIL", f)
    print("RESULT", "FAIL" if fails else "PASS", f"({len(fails)} fails)" if fails else "")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
