# -*- coding: utf-8 -*-
"""从 hf-mirror 抓 AISHELL-3 候选说话人样本 → 体检 → 拼 15~25s 参考音。
AISHELL-3: 44.1kHz/16bit 高保真棚录真人朗读(Apache 2.0)，43 男 175 女。
产物: downloads/aishell3_refs/<spk>_ref.wav + <spk>_ref.txt(参考文本) + 体检报告。
"""
import io, json, os, re, sys, time
import requests
import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _voice_clarity_audit import frame_rms_db, bandwidth_khz

MIRROR = "https://hf-mirror.com"
REPO = "datasets/shenyunhang/AISHELL-3"
OUT = r"C:\模仿音色\downloads\aishell3_refs"
os.makedirs(OUT, exist_ok=True)
H = {"User-Agent": "Mozilla/5.0"}

# 候选：男(gender=male)优先成熟声线(C=26-40),补 B 组；女 2 个作备选
SPEAKERS = {
    "SSB0710": ("male", "C", "north"),
    "SSB1100": ("male", "C", "south"),
    "SSB0273": ("male", "B", "north"),
    "SSB0966": ("male", "B", "north"),
    "SSB0629": ("male", "B", "north"),
    "SSB0534": ("female", "C", "north"),
    "SSB0016": ("female", "B", "north"),
}
N_FETCH = 10          # 每人抓前 N 条
TARGET_S = 20.0       # 拼接目标时长
GAP_S = 0.25


def get(url, binary=False, timeout=60):
    r = requests.get(url, headers=H, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.content if binary else r.text


def load_content_map():
    """content.txt: 'SSB00050001.wav\\t广 guang3 州 zhou1 ...' → {wav名: 纯汉字文本}"""
    cache = os.path.join(OUT, "content.txt")
    if not os.path.exists(cache):
        data = get(f"{MIRROR}/{REPO}/resolve/main/train/content.txt", binary=True, timeout=180)
        open(cache, "wb").write(data)
    mp = {}
    for ln in io.open(cache, encoding="utf-8", errors="replace"):
        parts = ln.strip().split("\t", 1)
        if len(parts) != 2:
            continue
        toks = parts[1].split()
        hanzi = "".join(t for t in toks if re.match(r"^[\u4e00-\u9fff]+$", t))
        mp[parts[0]] = hanzi
    return mp


def list_wavs(spk):
    j = json.loads(get(f"{MIRROR}/api/{REPO}/tree/main/train/wav/{spk}"))
    return [it["path"].split("/")[-1] for it in j if it.get("type") == "file" and it["path"].endswith(".wav")]


def audit_wav(x, sr):
    db = frame_rms_db(x, sr)
    voiced = db[db > db.max() - 35]
    snr = float(np.percentile(voiced, 90)) - float(np.percentile(db, 10))
    clip = float(np.mean(np.abs(x) > 0.985)) * 100
    return snr, clip


def main():
    content = load_content_map()
    print(f"content map: {len(content)} utterances", flush=True)
    report = []
    for spk, (gender, age, accent) in SPEAKERS.items():
        try:
            names = sorted(list_wavs(spk))[:N_FETCH]
        except Exception as e:
            print(f"{spk} LIST-FAIL {repr(e)[:80]}", flush=True)
            continue
        clips = []
        for nm in names:
            try:
                raw = get(f"{MIRROR}/{REPO}/resolve/main/train/wav/{spk}/{nm}", binary=True)
                x, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
                x = x.mean(axis=1)
                snr, clip = audit_wav(x, sr)
                txt = content.get(nm, "")
                clips.append(dict(nm=nm, x=x, sr=sr, snr=snr, clip=clip,
                                  dur=x.size / sr, txt=txt))
            except Exception as e:
                print(f"{spk}/{nm} DL-FAIL {repr(e)[:60]}", flush=True)
        if not clips:
            continue
        clips.sort(key=lambda c: -c["snr"])
        # 拼接：按 SNR 从高到低取，够 TARGET_S 即止(必须有转写文本才能给 ref_text)
        sel, total = [], 0.0
        for c in clips:
            if not c["txt"] or c["clip"] > 0.05:
                continue
            sel.append(c); total += c["dur"] + GAP_S
            if total >= TARGET_S:
                break
        if not sel:
            sel, total = clips[:5], sum(c["dur"] for c in clips[:5])
        sr = sel[0]["sr"]
        gap = np.zeros(int(sr * GAP_S), dtype=np.float32)
        pieces = []
        for c in sel:
            pieces.append(c["x"]); pieces.append(gap)
        y = np.concatenate(pieces[:-1])
        peak = float(np.abs(y).max()) or 1.0
        y = y * min(1.0, 0.9 / peak)                       # 统一响度上限,不压动态
        ref_wav = os.path.join(OUT, f"{spk}_ref.wav")
        sf.write(ref_wav, y, sr, subtype="PCM_16")
        ref_txt = "，".join(c["txt"] for c in sel)
        io.open(os.path.join(OUT, f"{spk}_ref.txt"), "w", encoding="utf-8").write(ref_txt)
        snr_r, clip_r = audit_wav(y, sr)
        bw = bandwidth_khz(y, sr)
        row = dict(spk=spk, gender=gender, age=age, accent=accent, sr=sr,
                   n=len(sel), dur=round(y.size / sr, 1), snr=round(snr_r, 1),
                   bw=round(bw, 1), clip=round(clip_r, 3),
                   snr_avg_raw=round(float(np.mean([c['snr'] for c in clips])), 1))
        report.append(row)
        print("DONE " + json.dumps(row, ensure_ascii=False), flush=True)
    report.sort(key=lambda r: -r["snr"])
    io.open(os.path.join(OUT, "report.json"), "w", encoding="utf-8").write(
        json.dumps(report, ensure_ascii=False, indent=1))
    print("REPORT " + json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
