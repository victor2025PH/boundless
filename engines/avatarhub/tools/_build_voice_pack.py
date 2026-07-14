# -*- coding: utf-8 -*-
"""AISHELL-3 全量真人音色包构建器（产品卖点：218 位真人参考音随选随用）。

每位说话人：抓前 N 条 → 逐条体检(信噪比/削波/有转写) → 挑最干净的拼 ~20s 参考音
→ 落 <spk>_ref.wav + <spk>_ref.txt + <spk>.meta.json(性别/年龄/口音/音质/声线特征)。
最后合并 index.json。男声排前(用户急着选男声)，female 随后。可断点续跑(已有 ref 的跳过)。

声线特征(选"有特点"用)：F0 中位数(低沉度)、F0 四分位距(语调起伏)、语速(字/秒)。
来源：hf-mirror 镜像 shenyunhang/AISHELL-3 (44.1kHz/16bit 棚录, Apache 2.0 可商用)。
"""
import io, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import requests
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _voice_clarity_audit import frame_rms_db, bandwidth_khz

MIRROR = "https://hf-mirror.com"
REPO = "datasets/shenyunhang/AISHELL-3"
OUT = r"C:\模仿音色\voice_pack_aishell3"
os.makedirs(OUT, exist_ok=True)
H = {"User-Agent": "Mozilla/5.0"}
N_FETCH = 10
TARGET_S = 20.0
GAP_S = 0.25
WORKERS = 6

_sess_local = threading.local()


def sess():
    if getattr(_sess_local, "s", None) is None:
        _sess_local.s = requests.Session()
        _sess_local.s.headers.update(H)
    return _sess_local.s


def get(url, binary=False, timeout=90, tries=3):
    for i in range(tries):
        try:
            r = sess().get(url, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            return r.content if binary else r.text
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def load_speakers():
    txt = get(f"{MIRROR}/{REPO}/resolve/main/spk-info.txt")
    spk = {}
    for ln in txt.splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        parts = ln.split()
        if len(parts) >= 4:
            spk[parts[0]] = {"age": parts[1], "gender": parts[2], "accent": parts[3]}
    return spk


def load_content(split):
    """content.txt → {wav名: 纯汉字文本}；train/test 各一份，磁盘缓存。"""
    cache = os.path.join(OUT, f"_content_{split}.txt")
    if not os.path.exists(cache):
        data = get(f"{MIRROR}/{REPO}/resolve/main/{split}/content.txt", binary=True, timeout=300)
        open(cache, "wb").write(data)
    mp = {}
    for ln in io.open(cache, encoding="utf-8", errors="replace"):
        parts = ln.strip().split("\t", 1)
        if len(parts) != 2:
            continue
        toks = parts[1].split()
        mp[parts[0]] = "".join(t for t in toks if re.match(r"^[\u4e00-\u9fff]+$", t))
    return mp


def list_split_dirs(split):
    j = json.loads(get(f"{MIRROR}/api/{REPO}/tree/main/{split}/wav"))
    return {it["path"].split("/")[-1] for it in j if it.get("type") == "directory"}


def f0_stats(x, sr):
    """轻量自相关 F0：中位数/四分位距(Hz)。仅用有声帧(能量高+周期性强)。"""
    win = int(sr * 0.04); hop = int(sr * 0.01)
    fmin, fmax = 60, 420
    lo, hi = int(sr / fmax), int(sr / fmin)
    f0s = []
    for i in range(0, x.size - win, hop):
        fr = x[i:i + win]
        e = float(np.sqrt(np.mean(fr ** 2)))
        if e < 0.02:
            continue
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, "full")[win - 1:]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi]
        if seg.size == 0:
            continue
        k = int(np.argmax(seg)) + lo
        if ac[k] / ac[0] < 0.45:                    # 周期性不足=清音/噪声帧
            continue
        f0s.append(sr / k)
    if len(f0s) < 10:
        return None, None
    f0s = np.array(f0s)
    return float(np.median(f0s)), float(np.percentile(f0s, 75) - np.percentile(f0s, 25))


def audit(x, sr):
    db = frame_rms_db(x, sr)
    voiced = db[db > db.max() - 35]
    snr = float(np.percentile(voiced, 90)) - float(np.percentile(db, 10))
    clip = float(np.mean(np.abs(x) > 0.985)) * 100
    return snr, clip


def build_speaker(spk, split, attrs, content):
    ref_wav = os.path.join(OUT, f"{spk}_ref.wav")
    meta_fp = os.path.join(OUT, f"{spk}.meta.json")
    if os.path.exists(ref_wav) and os.path.exists(meta_fp):
        return "skip"
    j = json.loads(get(f"{MIRROR}/api/{REPO}/tree/main/{split}/wav/{spk}"))
    names = sorted(it["path"].split("/")[-1] for it in j
                   if it.get("type") == "file" and it["path"].endswith(".wav"))[:N_FETCH]
    clips = []
    for nm in names:
        try:
            raw = get(f"{MIRROR}/{REPO}/resolve/main/{split}/wav/{spk}/{nm}", binary=True)
            x, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
            x = x.mean(axis=1)
            snr, clip = audit(x, sr)
            clips.append(dict(nm=nm, x=x, sr=sr, snr=snr, clip=clip,
                              dur=x.size / sr, txt=content.get(nm, "")))
        except Exception:
            pass
    if not clips:
        return "empty"
    clips.sort(key=lambda c: -c["snr"])
    sel, total = [], 0.0
    for c in clips:
        if not c["txt"] or c["clip"] > 0.05:
            continue
        sel.append(c); total += c["dur"] + GAP_S
        if total >= TARGET_S:
            break
    if not sel:
        sel = clips[:5]
    sr = sel[0]["sr"]
    gap = np.zeros(int(sr * GAP_S), dtype=np.float32)
    pieces = []
    for c in sel:
        pieces.append(c["x"]); pieces.append(gap)
    y = np.concatenate(pieces[:-1])
    peak = float(np.abs(y).max()) or 1.0
    y = y * min(1.0, 0.9 / peak)
    sf.write(ref_wav, y, sr, subtype="PCM_16")
    ref_txt = "，".join(c["txt"] for c in sel if c["txt"])
    io.open(os.path.join(OUT, f"{spk}_ref.txt"), "w", encoding="utf-8").write(ref_txt)
    snr, clip = audit(y, sr)
    f0_med, f0_iqr = f0_stats(y, sr)
    n_chars = sum(len(c["txt"]) for c in sel if c["txt"])
    speech_s = sum(c["dur"] for c in sel)
    meta = dict(spk=spk, split=split, **attrs, sr=sr, n_clips=len(sel),
                dur=round(y.size / sr, 1), snr=round(snr, 1),
                bw=round(bandwidth_khz(y, sr), 1), clip=round(clip, 3),
                f0_med=round(f0_med, 1) if f0_med else None,
                f0_iqr=round(f0_iqr, 1) if f0_iqr else None,
                rate=round(n_chars / speech_s, 2) if speech_s else None,
                ref_text=ref_txt)
    io.open(meta_fp, "w", encoding="utf-8").write(json.dumps(meta, ensure_ascii=False))
    return "ok"


def main():
    speakers = load_speakers()
    train_dirs = list_split_dirs("train")
    test_dirs = list_split_dirs("test")
    content = {}
    content.update(load_content("train"))
    try:
        content.update(load_content("test"))
    except Exception:
        print("WARN test content.txt 不可用(test 说话人无转写则跳过)", flush=True)
    jobs = []
    for spk, attrs in speakers.items():
        split = "train" if spk in train_dirs else ("test" if spk in test_dirs else None)
        if split:
            jobs.append((spk, split, attrs))
    jobs.sort(key=lambda t: (t[2]["gender"] != "male", t[0]))   # 男声优先
    print(f"TOTAL speakers={len(jobs)} male={sum(1 for j in jobs if j[2]['gender']=='male')}", flush=True)
    done = 0
    lock = threading.Lock()
    def run(job):
        nonlocal done
        spk, split, attrs = job
        try:
            st = build_speaker(spk, split, attrs, content)
        except Exception as e:
            st = f"fail {repr(e)[:60]}"
        with lock:
            done += 1
            print(f"[{done}/{len(jobs)}] {spk} {attrs['gender']} {st}", flush=True)
    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(run, jobs))
    # 合并 index
    rows = []
    for fn in os.listdir(OUT):
        if fn.endswith(".meta.json"):
            try:
                rows.append(json.load(io.open(os.path.join(OUT, fn), encoding="utf-8")))
            except Exception:
                pass
    rows.sort(key=lambda r: (r.get("gender") != "male", -(r.get("snr") or 0)))
    io.open(os.path.join(OUT, "index.json"), "w", encoding="utf-8").write(
        json.dumps(rows, ensure_ascii=False, indent=1))
    print(f"PACK-DONE speakers={len(rows)}", flush=True)


if __name__ == "__main__":
    main()
