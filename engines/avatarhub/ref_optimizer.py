# -*- coding: utf-8 -*-
"""参考音自动优化（带 holdout 验证，杜绝过拟合）。

流程：
  1. 候选池 = 角色现有 fish_refs（+ 可选新上传音频自动切段）
  2. 新音频按静音切成自然段，Whisper(STT 7854) 自动转写得参考文本
  3. 测试句拆 train/holdout 两组
  4. 在 train 上贪婪前向选择最佳多参考子集（合成→对说话人质心打分）
  5. 在 holdout 上对比「选中集」与「现有集」——仅当泛化更优(超过 margin)才推荐 apply

对外只依赖 numpy/scipy + 标准库 + clone_scorer + Fish/STT HTTP。
"""
import io
import wave
import json
import base64
import uuid
import urllib.request

import numpy as np

import clone_scorer as cs


# ── 音频工具 ───────────────────────────────────────────────
def _decode_wav(raw: bytes):
    with wave.open(io.BytesIO(raw), "rb") as w:
        nch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        fr = w.readframes(w.getnframes())
    if sw == 2:
        a = np.frombuffer(fr, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(fr, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 1:
        a = (np.frombuffer(fr, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported width {sw}")
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return a, sr


def _to_wav_b64(a: np.ndarray, sr: int) -> str:
    pcm = (np.clip(a, -1, 1) * 32767).astype(np.int16)
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return base64.b64encode(b.getvalue()).decode()


def _emb_b64(b64: str) -> np.ndarray:
    return cs._embed(cs._load_wav_16k(base64.b64decode(b64)))


def _b64_to_pcm_b64(b64: str) -> str:
    """任意 WAV b64 → 标准 16bit mono WAV b64（统一格式）。"""
    a, sr = _decode_wav(base64.b64decode(b64))
    return _to_wav_b64(a, sr)


# ── 切段（按静音）────────────────────────────────────────────
def segment_audio(a, sr, tgt_min=8.0, tgt_max=14.0, sil_rms=0.02, sil_min=0.30):
    fl = int(0.03 * sr)
    hop = int(0.01 * sr)
    if fl <= 0 or hop <= 0 or len(a) < fl:
        return [(0, len(a))]
    rms = np.array([np.sqrt(np.mean(a[i:i + fl] ** 2) + 1e-9)
                    for i in range(0, len(a) - fl, hop)])
    sil = rms < sil_rms
    cut = [0]
    i = 0
    minsil = int(sil_min / 0.01)
    while i < len(sil):
        if sil[i]:
            j = i
            while j < len(sil) and sil[j]:
                j += 1
            if j - i >= minsil:
                cut.append(int((i + j) / 2 * hop))
            i = j
        else:
            i += 1
    cut.append(len(a))
    cut = sorted(set(cut))
    segs = []
    s = cut[0]
    for k in range(1, len(cut)):
        e = cut[k]
        if (e - s) / sr >= tgt_min:
            if (e - s) / sr <= tgt_max or k == len(cut) - 1:
                segs.append((s, e))
                s = e
            else:
                mid = s + int(tgt_max * sr)
                segs.append((s, mid))
                s = mid
    if len(a) - s > sr * 3:
        segs.append((s, len(a)))
    elif segs:
        segs[-1] = (segs[-1][0], len(a))
    return segs or [(0, len(a))]


# ── STT / Fish HTTP ─────────────────────────────────────────
def transcribe(wav_b64: str, stt_url: str, language: str = "zh") -> str:
    boundary = uuid.uuid4().hex
    body = io.BytesIO()

    def w(s):
        body.write(s.encode() if isinstance(s, str) else s)

    raw = base64.b64decode(wav_b64)
    w(f"--{boundary}\r\n")
    w('Content-Disposition: form-data; name="audio"; filename="a.wav"\r\n')
    w("Content-Type: application/octet-stream\r\n\r\n")
    w(raw)
    w("\r\n")
    w(f"--{boundary}\r\n")
    w('Content-Disposition: form-data; name="language"\r\n\r\n')
    w(language)
    w("\r\n")
    w(f"--{boundary}--\r\n")
    rq = urllib.request.Request(
        stt_url.rstrip("/") + "/transcribe", data=body.getvalue(), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(rq, timeout=120) as r:
        return (json.loads(r.read()).get("text") or "").strip()


def _synth(fish_url, refs, text, params, seed):
    pay = {
        "text": text, "language": "zh",
        "temperature": params.get("temperature", 0.7),
        "top_p": params.get("top_p", 0.7),
        "repetition_penalty": params.get("repetition_penalty", 1.2),
        "chunk_length": params.get("chunk_length", 200),
        "seed": seed,
        "references": [{"audio_b64": r["voice_b64"], "text": r.get("text", "")}
                       for r in refs if r.get("voice_b64")],
    }
    rq = urllib.request.Request(
        fish_url.rstrip("/") + "/v1/tts/clone", data=json.dumps(pay).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(rq, timeout=180) as r:
        return json.loads(r.read()).get("audio_base64", "")


def _score_set(fish_url, refs, sents, target, emb_vb, params, seed):
    """合成各句，返回 (composite, centroid_mean, vb_mean, floor)。composite=0.5*质心+0.5*vb。"""
    cc, cv = [], []
    for s in sents:
        a = _synth(fish_url, refs, s, params, seed)
        if not a:
            continue
        e = _emb_b64(a)
        cc.append(float(np.dot(e, target)))
        cv.append(float(np.dot(e, emb_vb)))
    if not cc:
        return 0.0, 0.0, 0.0, 0.0
    mc = sum(cc) / len(cc)
    mv = sum(cv) / len(cv)
    return 0.5 * mc + 0.5 * mv, mc, mv, min(cc)


# ── 默认测试语料（train + holdout 两组互不相同）──────────────
_TRAIN = [
    "今天天气真不错，我们出去走走吧。",
    "人工智能正在改变我们的生活方式。",
    "这部电影的剧情非常精彩，值得一看。",
    "学习需要坚持，不能半途而废。",
]
_HOLDOUT = [
    "早上好，今天有什么计划吗。",
    "这本书我读了好几遍都不腻。",
    "坚持锻炼身体才会更健康。",
    "他的演讲赢得了全场热烈的掌声。",
]


def optimize(profile_data: dict, *, source_b64: str = "", stt_url: str,
             fish_url: str, max_refs: int = 4, margin: float = 0.01,
             train=None, holdout=None, seed: int = 123, progress_cb=None) -> dict:
    """返回 dict：候选、贪婪轨迹、train/holdout 分数、是否泛化更优(improved)、推荐参考集。"""
    def _p(msg):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    train = train or _TRAIN
    holdout = holdout or _HOLDOUT
    params = profile_data.get("fish_tts_params") or {}
    cur_refs = [dict(r) for r in (profile_data.get("fish_refs") or []) if r.get("voice_b64")]
    vb = profile_data.get("voice_b64", "")
    if not vb:
        return {"ok": False, "detail": "角色无参考音(voice_b64)，无法优化"}
    if not cur_refs and not source_b64:
        return {"ok": False, "detail": "无现有参考且未提供新音频"}

    _p("构建目标说话人质心…")
    emb_vb = _emb_b64(vb)
    centroid_embs = [emb_vb] + [_emb_b64(r["voice_b64"]) for r in cur_refs]
    target = np.mean(centroid_embs, axis=0)
    target /= (np.linalg.norm(target) + 1e-9)

    # 候选池
    pool = list(cur_refs)
    for _bi, _r in enumerate(pool):
        _r.setdefault("_src", f"cur#{_bi}")
    new_segments = []
    if source_b64:
        _p("切分新音频…")
        a, sr = _decode_wav(base64.b64decode(_b64_to_pcm_b64(source_b64)))
        spans = segment_audio(a, sr)
        for k, (s, e) in enumerate(spans):
            seg_b64 = _to_wav_b64(a[s:e], sr)
            _p(f"转写新段 {k + 1}/{len(spans)}…")
            txt = transcribe(seg_b64, stt_url)
            seg = {"voice_b64": seg_b64, "text": txt,
                   "_src": f"new[{s // sr}-{e // sr}s]"}
            pool.append(seg)
            new_segments.append({"span": f"{s / sr:.0f}-{e / sr:.0f}s", "text": txt})

    # 基线：现有参考
    _p("评估基线(现有参考)…")
    base_tr = _score_set(fish_url, cur_refs, train, target, emb_vb, params, seed) if cur_refs else (0,)*4
    base_ho = _score_set(fish_url, cur_refs, holdout, target, emb_vb, params, seed) if cur_refs else (0,)*4

    # 贪婪前向（在 train 上）
    _p("贪婪前向选择(train)…")
    chosen, chosen_idx = [], []
    remaining = list(range(len(pool)))
    best_tr = None
    trace = []
    while len(chosen) < max_refs and remaining:
        trials = []
        for i in remaining:
            sc = _score_set(fish_url, [pool[j] for j in chosen_idx + [i]],
                            train, target, emb_vb, params, seed)
            trials.append((sc[0], i, sc))
        trials.sort(reverse=True, key=lambda x: x[0])
        bs, bi, bsc = trials[0]
        if best_tr is not None and bs <= best_tr + 0.001:
            break
        chosen_idx.append(bi)
        chosen.append(pool[bi])
        remaining.remove(bi)
        best_tr = bs
        trace.append({"added": pool[bi].get("_src", f"cur#{bi}"),
                      "train_composite": round(bs, 4)})

    # holdout 验证：均值要超 margin，且最差句(floor)不得明显回归——双闸门防过拟合
    _p("holdout 验证…")
    rec_ho = _score_set(fish_url, chosen, holdout, target, emb_vb, params, seed)
    _floor_tol = 0.02
    mean_ok = (rec_ho[0] - base_ho[0]) >= margin
    floor_ok = (rec_ho[3] - base_ho[3]) >= -_floor_tol
    improved = bool(cur_refs) and mean_ok and floor_ok
    # 若无现有参考(纯新建)，只要 holdout 有分就接受
    if not cur_refs:
        improved = rec_ho[0] > 0
    reject_reason = ""
    if bool(cur_refs) and not improved:
        if not mean_ok:
            reject_reason = "均值提升不足"
        elif not floor_ok:
            reject_reason = "最差句回归(可能过拟合)"

    return {
        "ok": True,
        "improved": improved,
        "reject_reason": reject_reason,
        "margin": margin,
        "baseline": {"train_composite": round(base_tr[0], 4),
                     "holdout_composite": round(base_ho[0], 4),
                     "holdout_centroid": round(base_ho[1], 4),
                     "holdout_vb": round(base_ho[2], 4),
                     "holdout_floor": round(base_ho[3], 4)},
        "recommended": {"train_composite": round(best_tr or 0, 4),
                        "holdout_composite": round(rec_ho[0], 4),
                        "holdout_centroid": round(rec_ho[1], 4),
                        "holdout_vb": round(rec_ho[2], 4),
                        "holdout_floor": round(rec_ho[3], 4)},
        "holdout_delta": round(rec_ho[0] - base_ho[0], 4),
        "chosen": [r.get("_src", "cur") for r in chosen],
        "chosen_refs": [{"voice_b64": r["voice_b64"], "text": r.get("text", "")}
                        for r in chosen],
        "new_segments": new_segments,
        "trace": trace,
    }
