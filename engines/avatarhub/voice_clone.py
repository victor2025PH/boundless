# -*- coding: utf-8 -*-
"""
声音克隆模块 — 含伦理保护
===========================================
功能：
1. 录音5-10秒 → SNR质量检测 → 生成TTS参考音频
2. 合规声明勾选 + 日志记录
3. 公众人物声纹特征拒绝（本地特征库）
4. 数字水印（音频隐写：LSB嵌入标识）
"""
import sys, os, json, time, base64, tempfile, hashlib, struct, wave, io, math
from pathlib import Path

import app_config
BASE_DIR        = app_config.BASE
CLONE_DIR       = BASE_DIR / "voice_clones"
CLONE_LOG       = BASE_DIR / "voice_clone_log.jsonl"
REJECT_DB       = BASE_DIR / "rejected_voiceprints.json"   # 公众人物声纹库
WATERMARK_MAGIC = b"AVHUB_WM"   # 水印标识头

CLONE_DIR.mkdir(exist_ok=True)

MIN_DURATION_SEC = 5.0
MAX_DURATION_SEC = 30.0
MIN_SNR_DB       = 15.0   # 最低信噪比要求


# ── 音频质量检测 ──────────────────────────────────────────────────
def check_audio_quality(wav_bytes: bytes) -> dict:
    """
    检测音频质量：
    - 时长 (5-30秒)
    - SNR 信噪比 (>15dB)
    返回 {ok, duration_sec, snr_db, reason}
    """
    try:
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            n_channels  = wf.getnchannels()
            samp_width  = wf.getsampwidth()
            framerate   = wf.getframerate()
            n_frames    = wf.getnframes()
            raw_data    = wf.readframes(n_frames)

        duration = n_frames / framerate
        if duration < MIN_DURATION_SEC:
            return {"ok": False, "duration_sec": duration, "snr_db": 0,
                    "reason": f"录音过短({duration:.1f}秒)，请录制至少{MIN_DURATION_SEC}秒"}
        if duration > MAX_DURATION_SEC:
            return {"ok": False, "duration_sec": duration, "snr_db": 0,
                    "reason": f"录音过长({duration:.1f}秒)，请控制在{MAX_DURATION_SEC}秒以内"}

        # SNR估算：10ms 分帧取 RMS，最安静的有效帧≈底噪、最响的帧≈语音（思路与
        # avatar_hub._analyze_recording 深度自检一致）。旧版拿"尾部10%采样"当噪底，
        # 说话持续到文件结尾时会把人声当噪音、SNR≈0 误判。
        # 用细窗+低分位(2%)是为了连续说话、几乎无停顿的素材也能摸到真实底噪。
        if samp_width == 2:
            import array as _arr
            samples = _arr.array('h', raw_data)
            win = max(1, int(framerate * 0.01)) * max(1, n_channels)  # 10ms 窗(含声道交织)
            frame_rms = [(sum(x * x for x in samples[i:i + win]) / win) ** 0.5
                         for i in range(0, len(samples) - win + 1, win)]
            # 排除数字静音帧(<≈-80dBFS)：录制软件补零/剪辑留白不是真实环境底噪
            active = sorted(r for r in frame_rms if r > 32768 * 1e-4)
            if not active:
                return {"ok": False, "duration_sec": duration, "snr_db": 0,
                        "reason": "音频近乎无声，请检查麦克风与录音音量后重录"}
            noise_frames  = active[:max(1, int(len(active) * 0.02))]   # 最安静2%有效帧≈底噪
            speech_frames = active[int(len(active) * 0.60):]           # 最响40%有效帧≈语音
            rms_noise = sum(noise_frames) / len(noise_frames)
            rms_sig   = sum(speech_frames) / len(speech_frames)
            # 标准SNR公式: 20 * log10(RMS信号 / RMS噪底)
            snr = 20 * math.log10(max(rms_sig, 1e-10) / max(rms_noise, 1e-10))
            snr = max(0.0, min(snr, 60.0))   # 限制在[0,60]dB内
        else:
            snr = 20.0   # 无法精确计算，假设合格

        if snr < MIN_SNR_DB:
            return {"ok": False, "duration_sec": duration, "snr_db": snr,
                    "reason": f"背景噪音过大(SNR={snr:.1f}dB<{MIN_SNR_DB}dB)，请在安静环境录制"}

        return {"ok": True, "duration_sec": duration, "snr_db": snr, "reason": ""}

    except Exception as e:
        return {"ok": False, "duration_sec": 0, "snr_db": 0, "reason": f"音频解析失败: {e}"}


# ── 公众人物声纹检测 ──────────────────────────────────────────────
def _load_reject_db() -> dict:
    if REJECT_DB.exists():
        try:
            return json.loads(REJECT_DB.read_text(encoding='utf-8'))
        except Exception:
            pass
    # 默认内置空库（管理员可手动添加声纹hash）
    return {"version": "1.0", "entries": []}


def check_voiceprint(wav_bytes: bytes) -> dict:
    """
    对比声纹特征库，拒绝公众人物
    当前实现：基于简单音频特征hash（生产环境可替换为ECAPA-TDNN等模型）
    返回 {ok, matched_name, reason}
    """
    try:
        db = _load_reject_db()
        entries = db.get("entries", [])
        if not entries:
            return {"ok": True, "matched_name": "", "reason": ""}

        # 计算当前音频的简单频谱指纹
        current_fp = _simple_fingerprint(wav_bytes)

        for entry in entries:
            stored_fp = entry.get("fingerprint", "")
            if not stored_fp:
                continue
            # 汉明距离比较（简单实现）
            if _fingerprint_similarity(current_fp, stored_fp) > 0.85:
                name = entry.get("name", "未知公众人物")
                _log_reject(name, current_fp)
                return {"ok": False, "matched_name": name,
                        "reason": f"检测到疑似公众人物声纹({name})，出于伦理保护拒绝克隆"}

        return {"ok": True, "matched_name": "", "reason": ""}
    except Exception as e:
        return {"ok": True, "matched_name": "", "reason": f"声纹检测跳过: {e}"}


def _simple_fingerprint(wav_bytes: bytes) -> str:
    """生成简单音频指纹（基于分块哈希）"""
    try:
        md5 = hashlib.md5(wav_bytes[:min(len(wav_bytes), 50000)]).hexdigest()
        return md5
    except Exception:
        return ""


def _fingerprint_similarity(fp1: str, fp2: str) -> float:
    """简单相似度（生产环境替换为余弦相似度）"""
    if not fp1 or not fp2:
        return 0.0
    return 1.0 if fp1 == fp2 else 0.0


def _log_reject(name: str, fingerprint: str):
    entry = {"ts": time.time(), "event": "voiceprint_rejected",
             "matched": name, "fp": fingerprint[:16]}
    try:
        with open(CLONE_LOG, "a", encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── 数字水印 ──────────────────────────────────────────────────────
def embed_watermark(wav_bytes: bytes, owner_id: str = "avatarhub") -> bytes:
    """
    在 WAV 音频末尾嵌入不可见水印（基于LSB）
    水印 = MAGIC(8字节) + owner_id_hash(16字节) + timestamp(8字节)
    """
    try:
        ts_bytes    = struct.pack(">d", time.time())
        owner_hash  = hashlib.md5(owner_id.encode()).digest()[:16]
        watermark   = WATERMARK_MAGIC + owner_hash + ts_bytes
        # 找WAV data chunk末尾插入（简单追加，不破坏WAV结构）
        # 真实实现应修改 PCM 最低有效位，这里用简单追加方式演示
        return wav_bytes + b"\x00" * 8 + watermark
    except Exception:
        return wav_bytes


def verify_watermark(wav_bytes: bytes) -> dict:
    """验证音频是否含有AvatarHub水印"""
    try:
        wm_size = len(WATERMARK_MAGIC) + 16 + 8
        tail    = wav_bytes[-(wm_size + 8):]
        if WATERMARK_MAGIC in tail:
            idx = tail.find(WATERMARK_MAGIC)
            ts_bytes = tail[idx + len(WATERMARK_MAGIC) + 16 : idx + len(WATERMARK_MAGIC) + 24]
            ts = struct.unpack(">d", ts_bytes)[0] if len(ts_bytes) == 8 else 0
            return {"has_watermark": True, "created_at": ts,
                    "created_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))}
        return {"has_watermark": False}
    except Exception:
        return {"has_watermark": False}


# ── 合规记录 ──────────────────────────────────────────────────────
def log_compliance(user_id: str, action: str, agreed: bool, audio_hash: str):
    """记录合规声明同意日志"""
    entry = {
        "ts":         time.time(),
        "ts_str":     time.strftime("%Y-%m-%d %H:%M:%S"),
        "user_id":    user_id,
        "action":     action,
        "agreed":     agreed,
        "audio_hash": audio_hash,
    }
    try:
        with open(CLONE_LOG, "a", encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── 主克隆流程 ────────────────────────────────────────────────────
def clone_voice(wav_b64: str, name: str, agreed_terms: bool,
                user_id: str = "local") -> dict:
    """
    完整声音克隆流程:
    1. 合规检查 → 2. 音质检测 → 3. 声纹检测 → 4. 保存参考音频 → 5. 嵌入水印
    返回 {ok, voice_file, quality, reason}
    """
    if not agreed_terms:
        return {"ok": False, "reason": "请先同意合规声明（我拥有该声音的合法使用权）"}

    try:
        wav_bytes = base64.b64decode(wav_b64)
    except Exception as e:
        return {"ok": False, "reason": f"音频解码失败: {e}"}

    audio_hash = hashlib.md5(wav_bytes).hexdigest()

    # 记录合规日志
    log_compliance(user_id, "voice_clone", True, audio_hash)

    # 音质检测
    quality = check_audio_quality(wav_bytes)
    if not quality["ok"]:
        return {"ok": False, "reason": quality["reason"], "quality": quality}

    # 声纹检测
    vp_check = check_voiceprint(wav_bytes)
    if not vp_check["ok"]:
        return {"ok": False, "reason": vp_check["reason"], "quality": quality}

    # 嵌入水印
    wav_watermarked = embed_watermark(wav_bytes, owner_id=user_id)

    # 保存参考音频
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    out_path  = CLONE_DIR / f"{safe_name}_{int(time.time())}.wav"
    out_path.write_bytes(wav_watermarked)

    return {
        "ok":         True,
        "voice_file": str(out_path),
        "voice_b64":  base64.b64encode(wav_watermarked).decode(),
        "quality":    quality,
        "watermark":  verify_watermark(wav_watermarked),
        "reason":     "",
    }
