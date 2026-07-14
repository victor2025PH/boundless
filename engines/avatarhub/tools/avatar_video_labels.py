# -*- coding: utf-8 -*-
"""P2-VL: avatar_videos/ 哈希名底视频对账——生成 _labels.json 显示名映射（文件名零改动）。

av_<hash>_* 是试衣/微动管线落盘的机器名，人翻文件夹对不上角色。此工具：
1. 按前缀聚合成套（_idle_loop.mp4 / _body.mp4 / _face.jpg / _src.*）；
2. 反查全部角色的 idle_video/body_video 绑定 → 每套标注归属角色（=显示名）；
3. 套内关键文件 md5 → 找重复套（同内容不同哈希名，历史管线重复落盘）；
4. 产出 avatar_videos/_labels.json + 控制台孤儿/重复报告。只写清单，不删不改名。
"""
import hashlib
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
HUB = "http://127.0.0.1:9000"
VID_DIR = Path(r"C:\模仿音色\avatar_videos")
OUT = VID_DIR / "_labels.json"
SUFFIXES = ("_idle_loop.mp4", "_body.mp4", "_face.jpg", "_src.mp4", "_src.mov", "_src.avi")


def md5_of(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    sets = defaultdict(dict)
    for f in VID_DIR.iterdir():
        if not f.is_file():
            continue
        for suf in SUFFIXES:
            if f.name.endswith(suf):
                sets[f.name[: -len(suf)]][suf] = f.name
                break

    # 角色绑定反查（detail 才有 idle_video/body_video）
    refs = defaultdict(set)
    d = json.loads(urllib.request.urlopen(f"{HUB}/profiles", timeout=10).read())
    for p in d["profiles"]:
        det = json.loads(urllib.request.urlopen(
            f"{HUB}/profiles/{urllib.parse.quote(p['name'])}", timeout=10).read())
        prof = det.get("profile", det)
        for field in ("idle_video", "body_video"):
            v = (prof.get(field) or "").replace("\\", "/")
            m = re.match(r"avatar_videos/(.+)", v)
            if m:
                for suf in SUFFIXES:
                    if m.group(1).endswith(suf):
                        refs[m.group(1)[: -len(suf)]].add(p["name"])

    # 套内容指纹：核心三件（idle/body/face）逐文件 md5 拼合
    fp = {}
    for prefix, files in sets.items():
        core = [files.get(s) for s in ("_idle_loop.mp4", "_body.mp4", "_face.jpg")]
        fp[prefix] = "|".join(md5_of(VID_DIR / n) if n else "-" for n in core)
    dup_of = {}
    seen = {}
    for prefix in sorted(sets, key=lambda x: (x not in refs, x)):   # 有归属的当正主
        if fp[prefix] in seen:
            dup_of[prefix] = seen[fp[prefix]]
        else:
            seen[fp[prefix]] = prefix

    out = {}
    for prefix, files in sorted(sets.items()):
        r = sorted(refs.get(prefix, ()))
        out[prefix] = {
            "label": "、".join(r) if r else "",
            "refs": r,
            "files": sorted(files.values()),
            "orphan": not r,
            **({"dup_of": dup_of[prefix]} if prefix in dup_of else {}),
        }
    OUT.write_text(json.dumps({
        "comment": "底视频套显示名映射（工具生成，勿手编）。label=绑定角色；orphan=无角色引用；"
                   "dup_of=内容与另一套完全一致（md5），清理时优先删孤儿重复套",
        "ts": int(time.time()), "sets": out}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{len(out)} 套 → {OUT}")
    for prefix, e in out.items():
        mark = "🟢" if e["refs"] else ("♻️ 重复孤儿" if "dup_of" in e else "🟡 孤儿")
        extra = f" =内容同 {e['dup_of']}" if "dup_of" in e else ""
        print(f"  {mark} {prefix:<18} → {e['label'] or '（无角色引用）'}{extra}")


if __name__ == "__main__":
    main()
