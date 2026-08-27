# -*- coding: utf-8 -*-
"""voice_match 回填（实施58 P3-1 一次性 ops 工具，只改 sidecar/take_json）。

给已在货架的唱段补量「vs 本人说话声」的 campplus 贴合度：
  - 曲库存货：<数据根>/assets/voices/<pid>/songs/<tid>.json 增 voice_match；
  - 专属歌订单：orders.take_json 增 voice_match（有音频的单）。
音频是 ogg → ffmpeg 解成 wav 再喂评分器。幂等（已有值跳过，--force 重算）。
用法：python tools/backfill_voice_match.py [--force] [--data-root ...]
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import song_factory as sf  # noqa: E402
from scripts._data_root import resolve_data_roots  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def to_wav(audio: bytes, suffix: str) -> bytes | None:
    if suffix == ".wav":
        return audio
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    with tempfile.TemporaryDirectory(prefix="vmbf_") as td:
        src, dst = Path(td) / f"in{suffix}", Path(td) / "out.wav"
        src.write_bytes(audio)
        r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(src),
                            "-ar", "44100", "-ac", "1", str(dst)],
                           capture_output=True, timeout=120)
        return dst.read_bytes() if r.returncode == 0 and dst.is_file() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--data-root", default="")
    args = ap.parse_args()
    root = resolve_data_roots(args.data_root)[0]
    print(f"[*] 数据根 {root}")

    done = skip = fail = 0
    # 1) 曲库存货
    sroot = root / "assets" / "voices"
    for pdir in sorted(sroot.iterdir() if sroot.is_dir() else []):
        sdir = pdir / "songs"
        if not sdir.is_dir():
            continue
        for sc in sorted(sdir.glob("*.json")):
            try:
                meta = json.loads(sc.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not args.force and meta.get("voice_match") is not None:
                skip += 1
                continue
            audio = None
            for ext in (".ogg", ".wav"):
                p = sdir / f"{sc.stem}{ext}"
                if p.is_file():
                    audio = (p.read_bytes(), ext)
                    break
            if audio is None:
                continue
            wav = to_wav(*audio)
            vm = sf.voice_match(pdir.name, wav) if wav else None
            meta["voice_match"] = vm
            sc.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                          encoding="utf-8")
            print(f"  {pdir.name}/{sc.stem}: voice_match={vm}")
            done += 1
            if vm is None:
                fail += 1
    # 2) 专属歌订单
    try:
        from src.companion.song_orders import SongOrderStore
        store = SongOrderStore(root=root)
        for row in store.list_orders(limit=200):
            take = {}
            try:
                take = json.loads(row.get("take_json") or "{}") or {}
            except Exception:
                pass
            ap_path = str(row.get("audio_path") or "")
            if not ap_path or (not args.force
                               and take.get("voice_match") is not None):
                continue
            p = Path(ap_path)
            if not p.is_file():
                continue
            wav = to_wav(p.read_bytes(), p.suffix)
            vm = sf.voice_match(str(row.get("persona_id")), wav) if wav else None
            take["voice_match"] = vm
            import sqlite3
            with sqlite3.connect(str(store.path)) as c:
                c.execute("UPDATE orders SET take_json=? WHERE id=?",
                          (json.dumps(take, ensure_ascii=False), row["id"]))
            print(f"  order#{row['id']}: voice_match={vm}")
            done += 1
    except Exception as e:  # noqa: BLE001
        print(f"[!] 订单回填软失败: {e}")
    print(f"[*] done={done} skip={skip} scorer_miss={fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
