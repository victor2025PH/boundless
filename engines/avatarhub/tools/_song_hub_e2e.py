# -*- coding: utf-8 -*-
"""Song-P1 端到端验收：走 Hub /api/song/cover 全链路（上传→任务→轮询→成品→水印/历史/贴合度）。"""
import io
import json
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]
HUB = "http://127.0.0.1:9000"
SONG = BASE / "YingMusic-SVC" / "accom_separation" / "samples" / "raw" / "All I Want For Christmas Is You01.MP3"
REF = BASE / "CosyVoice" / "asset" / "zero_shot_prompt.wav"


def main():
    r = requests.get(f"{HUB}/api/song/health", timeout=10)
    print("health:", r.json())
    files = {
        "song": (SONG.name, SONG.read_bytes(), "audio/mpeg"),
        "reference": (REF.name, REF.read_bytes(), "audio/wav"),
    }
    data = {"profile": "e2e_spike", "pitch": "auto", "quality": "standard"}
    r = requests.post(f"{HUB}/api/song/cover", files=files, data=data, timeout=120)
    print("submit:", r.status_code, r.text[:300])
    r.raise_for_status()
    tid = r.json()["task_id"]

    t0 = time.time()
    fin = None
    while time.time() - t0 < 600:
        st = requests.get(f"{HUB}/api/song/task/{tid}", timeout=30).json()
        print(f"[{time.time()-t0:5.0f}s] {st.get('status')} {st.get('progress')}% {st.get('detail','')}")
        if st.get("status") in ("done", "error", "cancelled"):
            fin = st
            break
        time.sleep(5)

    if not fin or fin.get("status") != "done":
        print("FAILED:", json.dumps(fin, ensure_ascii=False)[:500])
        return 1

    keys = sorted(fin.keys())
    print("final keys:", keys)
    res = fin.get("result") or {}
    print("result meta:", {k: v for k, v in res.items() if k != 'audio_base64'})
    print("similarity:", fin.get("similarity"))
    print("watermark:", bool(fin.get("watermark") or res.get("watermark")))
    print("audio_b64 len:", len(fin.get("audio_base64") or res.get("audio_base64") or ""))

    # 历史落库验证（响应形如 {ok, records, count, total_count}）
    h = requests.get(f"{HUB}/api/history", params={"limit": 5}, timeout=10)
    if h.status_code == 200:
        items = h.json().get("records", [])
        hits = [it for it in items if "翻唱" in (it.get("text") or "")]
        print("history hit:", len(hits) > 0, (hits[0].get("text", "")[:60] if hits else ""))
    else:
        print("history probe:", h.status_code)

    # 成品直链可播验证（audio.wav 原始字节）
    url = fin.get("audio_url") or ""
    if url:
        a = requests.get(f"{HUB}{url}", timeout=30)
        ct = a.headers.get("content-type", "")
        print("audio.wav:", a.status_code, ct, len(a.content), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
