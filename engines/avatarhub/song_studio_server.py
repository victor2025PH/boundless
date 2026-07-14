# -*- coding: utf-8 -*-
"""
Song Studio — AI 翻唱服务（端口 7853，conda env: ymsvc）

引擎：YingMusic-SVC（MIT，2025-11 开源）零样本歌声转换
  管线：整曲 → BS-RoFormer 人声分离 → SVC 音色转换（自动升降调）→ 与伴奏混音
  参考音色：3~30 秒任意人声（角色克隆参考音直接可用，无需逐人训练）

设计要点：
  * 权重全部走本地绝对路径（models/song_studio/），运行期零 HF 网络依赖；
  * 分离/转换按阶段懒加载，任务后默认卸载（SONG_KEEP_LOADED=1 关闭卸载），
    与实时对话三件套共存于 32GB 5090；
  * 单 worker 线程串行消费任务队列（GPU 串行化），任务间/转换分块间可协作取消；
  * /health 返回能力旗标：weights 不齐时 capabilities.cover=False（Hub/前端诚实降级），
    绝不出现「假在线」。

历史包袱：7853 原是 GPT-SoVITS singing_server（运行时 2026-06 已清空，
/v1/tts/sing 在本服务返回 404 → hub /avatar/sing 自动落到念白链，行为正确）。
"""
import base64
import gc
import io
import logging
import hashlib
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

BASE = Path(__file__).resolve().parent
YM_ROOT = BASE / "YingMusic-SVC"
SEP_ROOT = YM_ROOT / "accom_separation"
MODELS = BASE / "models" / "song_studio"

# accom_separation 在前：其 utils/models 包名与根仓 utils 冲突，SVC 链不用根 utils（已核）
for _p in (str(SEP_ROOT), str(YM_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("song_studio")

PORT = int(os.environ.get("SONG_STUDIO_PORT", "7853"))
KEEP_LOADED = os.environ.get("SONG_KEEP_LOADED", "0") == "1"
MAX_SONG_SEC = int(os.environ.get("SONG_MAX_SEC", "480"))        # 8 分钟
MAX_UPLOAD_MB = int(os.environ.get("SONG_MAX_UPLOAD_MB", "40"))  # 解码前字节
TASK_TTL_SEC = 3600 * 6                                          # 产物保留 6h

# Song-P3/O6: 直播让路——重活(分离/SVC)开工前问 Hub「现在直播/对话忙不忙」，忙则挂起等
# （任务保持 queued+人话 detail，直播结束自动继续）。Hub 不可达=不让路（fail-open，
# 独立跑引擎/Hub 挂了都不影响唱歌）。SONG_LIVE_YIELD=0 彻底关闭。
HUB_URL = os.environ.get("SONG_HUB_URL", "http://127.0.0.1:9000").rstrip("/")
LIVE_YIELD = os.environ.get("SONG_LIVE_YIELD", "1") != "0"
YIELD_POLL_S = float(os.environ.get("SONG_YIELD_POLL_S", "5"))
_YIELD_LAST = {"yield": False, "reason": "", "ts": 0.0}          # /health 可观测

W = {
    "svc":      MODELS / "YingMusic-SVC-full.pt",
    "sep":      MODELS / "bs_roformer.ckpt",
    "sep_cfg":  SEP_ROOT / "ckpt" / "bs_roformer" / "config_bd_roformer.yaml",
    # Song-P3/O2: Mel-Band RoFormer（Kim 社区权重）——精细档分离模型，缺权重时自动回退 BS
    "sep_mel":     MODELS / "mel_band_roformer.ckpt",
    "sep_mel_cfg": SEP_ROOT / "ckpt" / "mel_band_roformer" / "config_vocals_mel_band_roformer_kj.yaml",
    "svc_cfg":  YM_ROOT / "configs" / "YingMusic-SVC.yml",
    "rmvpe":    MODELS / "rmvpe.pt",
    "campplus": MODELS / "campplus_cn_common.bin",
    "bigvgan":  MODELS / "bigvgan_v2_44khz_128band_512x",
    "whisper":  MODELS / "whisper-small",
}


def _weights_ok() -> dict:
    return {k: p.exists() for k, p in W.items()}


def _cap() -> dict:
    ok = _weights_ok()
    svc_ready = all(ok[k] for k in ("svc", "svc_cfg", "rmvpe", "campplus",
                                    "bigvgan", "whisper"))
    sep_ready = ok["sep"] and ok["sep_cfg"]
    return {
        "cover": svc_ready and sep_ready,      # 整曲翻唱（分离+转换+混音）
        "svc": svc_ready,                      # 干声直转
        "separate": sep_ready,                 # 人声分离
        "separate_mel": ok["sep_mel"] and ok["sep_mel_cfg"],  # P3/O2: 精细档 Mel-Band 分离
        "sing_from_lyrics": False,             # 歌词直接成曲（SVS）尚未部署 — 诚实上报
    }


# ─────────────────────────── 音频 IO ───────────────────────────

def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def load_audio_any(data: bytes, target_sr: int, mono: bool):
    """任意容器（wav/mp3/flac/m4a…）→ float32 ndarray。
    soundfile 优先；不支持的封装走 ffmpeg 解码兜底。
    返回 (audio[C,T] 或 [T], sr)。"""
    import numpy as np
    import soundfile as sf
    try:
        arr, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        arr = arr.T                                     # [C, T]
    except Exception:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.bin")
            dst = os.path.join(td, "out.wav")
            with open(src, "wb") as f:
                f.write(data)
            r = subprocess.run(
                [_ffmpeg_exe(), "-y", "-i", src, "-ar", str(target_sr),
                 "-acodec", "pcm_s16le", dst],
                capture_output=True, timeout=300)
            if r.returncode != 0 or not os.path.exists(dst):
                raise ValueError("音频解码失败：不支持的格式或文件损坏")
            arr, sr = sf.read(dst, dtype="float32", always_2d=True)
            arr = arr.T
    if sr != target_sr:
        import librosa
        arr = librosa.resample(arr, orig_sr=sr, target_sr=target_sr)
        sr = target_sr
    if mono and arr.shape[0] > 1:
        arr = arr.mean(axis=0, keepdims=True)
    return (arr[0] if mono else arr), sr


def _wav_bytes(arr, sr: int) -> bytes:
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, arr.T if arr.ndim == 2 else arr, sr,
             format="WAV", subtype="PCM_16")
    return buf.getvalue()


# ─────────────────────────── 模型加载（懒） ───────────────────────────

_load_lock = threading.Lock()
_sep_bundles: dict = {}   # kind("bs"/"mel") → (model, config)  P3/O2: 双分离模型
_svc_bundle = None        # dict

# 分离模型登记：kind → (model_type, ckpt路径键, cfg路径键, 展示名)
_SEP_KINDS = {
    "bs":  ("bs_roformer",       "sep",     "sep_cfg",     "BS-RoFormer"),
    "mel": ("mel_band_roformer", "sep_mel", "sep_mel_cfg", "Mel-Band RoFormer"),
}


def _device():
    import torch
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _load_sep(kind: str = "bs"):
    """P3/O2: kind=bs(默认/标准档) | mel(精细档, Kim Mel-Band)。同一时刻只驻留一个分离模型
    （换 kind 先卸另一个，避免两模型同时占显存）。"""
    if kind not in _SEP_KINDS:
        kind = "bs"
    b = _sep_bundles.get(kind)
    if b is not None:
        return b
    with _load_lock:
        b = _sep_bundles.get(kind)
        if b is not None:
            return b
        import argparse
        import torch
        from utils.settings import get_model_from_config          # accom_separation/utils
        from utils.model_utils import load_start_checkpoint
        model_type, wk, ck, label = _SEP_KINDS[kind]
        for other in list(_sep_bundles):          # 互斥驻留：先卸旧模型再载新
            if other != kind:
                _sep_bundles.pop(other, None)
        t0 = time.time()
        logger.info(f"加载 {label} 分离模型...")
        model, config = get_model_from_config(model_type, str(W[ck]))
        ckpt = torch.load(str(W[wk]), weights_only=False, map_location="cpu")
        args = argparse.Namespace(start_check_point=str(W[wk]),
                                  model_type=model_type, lora_checkpoint="")
        load_start_checkpoint(args, model, ckpt, type_="inference")
        model = model.to(_device()).eval()
        _sep_bundles[kind] = (model, config)
        logger.info(f"分离模型就绪 {label} ({time.time()-t0:.1f}s)")
        return _sep_bundles[kind]


def _load_svc():
    """按 my_inference.load_models_api 改造：权重全走本地路径，零 HF 网络。"""
    global _svc_bundle
    if _svc_bundle is not None:
        return _svc_bundle
    with _load_lock:
        if _svc_bundle is not None:
            return _svc_bundle
        import torch
        import yaml
        t0 = time.time()
        logger.info("加载 YingMusic-SVC 管线...")
        device = _device()
        from modules.commons import recursive_munch, build_model, load_checkpoint
        from modules.rmvpe import RMVPE
        from modules.campplus.DTDNN import CAMPPlus
        from modules.bigvgan import bigvgan
        from modules.audio import mel_spectrogram
        from transformers import AutoFeatureExtractor, WhisperModel

        config = yaml.safe_load(open(W["svc_cfg"], "r", encoding="utf-8"))
        model_params = recursive_munch(config["model_params"])
        model_params.dit_type = "DiT"
        model = build_model(model_params, stage="DiT")
        model, _, _, _ = load_checkpoint(model, None, str(W["svc"]),
                                         load_only_params=True,
                                         ignore_modules=[], is_distributed=False)
        for key in model:
            model[key].eval()
            model[key].to(device)
        model.cfm.estimator.setup_caches(max_batch_size=1, max_seq_length=8192)

        f0_extractor = RMVPE(str(W["rmvpe"]), is_half=False, device=device)

        campplus = CAMPPlus(feat_dim=80, embedding_size=192)
        campplus.load_state_dict(torch.load(str(W["campplus"]), map_location="cpu"))
        campplus.eval().to(device)

        vocoder = bigvgan.BigVGAN.from_pretrained(str(W["bigvgan"]),
                                                  use_cuda_kernel=False)
        vocoder.remove_weight_norm()
        vocoder = vocoder.eval().to(device)

        whisper = WhisperModel.from_pretrained(str(W["whisper"]),
                                               torch_dtype=torch.float16).to(device)
        del whisper.decoder
        feat_extractor = AutoFeatureExtractor.from_pretrained(str(W["whisper"]))

        spect = config["preprocess_params"]["spect_params"]
        mel_args = {
            "n_fft": spect["n_fft"], "win_size": spect["win_length"],
            "hop_size": spect["hop_length"], "num_mels": spect["n_mels"],
            "sampling_rate": config["preprocess_params"]["sr"],
            "fmin": spect.get("fmin", 0),
            "fmax": None if spect.get("fmax", "None") == "None" else 8000,
            "center": False,
        }
        _svc_bundle = {
            "model": model, "config": config, "f0_fn": f0_extractor.infer_from_audio,
            "campplus": campplus, "vocoder": vocoder, "whisper": whisper,
            "feat_extractor": feat_extractor,
            "mel_fn": (lambda x: mel_spectrogram(x, **mel_args)),
            "sr": config["preprocess_params"]["sr"],
            "use_style_residual": config["model_params"]["length_regulator"]
                                  .get("use_style_residual", False),
        }
        logger.info(f"SVC 管线就绪 ({time.time()-t0:.1f}s)")
        return _svc_bundle


def _unload(which: str = "all"):
    global _svc_bundle
    import torch
    with _load_lock:
        if which in ("sep", "all"):
            _sep_bundles.clear()
        if which in ("svc", "all"):
            _svc_bundle = None
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


# ─────────────────────────── 推理阶段 ───────────────────────────

class CancelledError(RuntimeError):
    pass


def _yield_probe() -> dict:
    """问 Hub 直播让路状态。异常/关闭 → 不让路。"""
    if not LIVE_YIELD:
        return {"yield": False, "reason": "", "source": ""}
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(HUB_URL + "/api/song/yield", timeout=4) as r:
            j = _json.loads(r.read().decode("utf-8"))
        st = {"yield": bool(j.get("yield")), "reason": str(j.get("reason", "")),
              "source": str(j.get("source", ""))}
    except Exception:
        st = {"yield": False, "reason": "", "source": ""}
    _YIELD_LAST.update(st, ts=time.time())
    return st


# P4: vram 源的让路只等一小会儿：显存高压若非直播（live/converse/hold 单独成源），
# 多半是闲置引擎常驻占卡——死等等不来，放行让 _ensure_vram 去「请 Hub 腾闲置引擎」。
# （2026-07-07 真跑实锤：31.5G 被闲置 latentsync 等占满，任务在 vram 让路里挂了 20 分钟）
VRAM_YIELD_GRACE_S = float(os.environ.get("SONG_VRAM_YIELD_GRACE_S", "30"))
# 分离+SVC 串行峰值 ~4.5GB（含模型互斥驻留）；低于此水位先请 Hub 腾闲置引擎
SONG_MIN_FREE_MB = int(os.environ.get("SONG_MIN_FREE_MB", "5500"))


def _yield_wait(tid: str, t: dict) -> int:
    """P3/O6: 直播/对话进行中 → 重活挂起（GPU 一点不碰），结束自动继续。可取消。
    返回等待毫秒数。P4: vram 源超宽限即放行（交给显存清场，不死等）。"""
    st = _yield_probe()
    if not st["yield"]:
        return 0
    t0 = time.time()
    logger.info(f"[{tid}] 直播让路挂起: {st['reason']}")
    while st["yield"]:
        if st.get("source") == "vram" and time.time() - t0 > VRAM_YIELD_GRACE_S:
            logger.info(f"[{tid}] 让路源=显存高压且非直播，转交显存清场处理")
            break
        _set(tid, status="queued",
             detail=f"直播让路中（{st['reason'] or '直播进行中'}）——不抢直播算力，结束后自动继续")
        for _ in range(int(max(1.0, YIELD_POLL_S))):
            if t.get("cancel"):
                raise CancelledError()
            time.sleep(1.0)
        st = _yield_probe()
    waited = int((time.time() - t0) * 1000)
    if waited > 1000:
        logger.info(f"[{tid}] 让路 {waited/1000:.0f}s 后继续")
    return waited


def _vram_free_mb() -> int:
    try:
        import torch
        if not torch.cuda.is_available():
            return 1 << 20            # 无 CUDA（CPU 跑）：不拦
        free, _ = torch.cuda.mem_get_info()
        return int(free / (1024 * 1024))
    except Exception:
        return 1 << 20                # 探测失败不拦（fail-open，交给加载去试）


def _ensure_vram(tid: str, t: dict) -> None:
    """P4: 加载前显存预检——不直播但闲置引擎占卡时，请 Hub free_unused（只停
    本模式用不到的闲置引擎，核心不动），仍不够则人话报错——绝不裸 OOM。"""
    if _svc_bundle is not None or _sep_bundles:
        return                        # 模型已驻留（KEEP_LOADED）：显存已占好，不用腾
    free = _vram_free_mb()
    if free >= SONG_MIN_FREE_MB:
        return
    logger.info(f"[{tid}] 显存不足({free}MB < {SONG_MIN_FREE_MB}MB)，请 Hub 腾显存(free_unused)")
    _set(tid, status="queued", detail="显存吃紧——正在请闲置引擎腾位（不影响直播核心）")
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(HUB_URL + "/api/gpu/free_unused", method="POST",
                                     data=b"", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            _json.loads(r.read().decode("utf-8"))
    except Exception as e:
        logger.warning(f"[{tid}] free_unused 调用失败(继续按现状检查): {e}")
    for _ in range(10):               # 停服务→显存归还有秒级延迟，最多等 20s
        if t.get("cancel"):
            raise CancelledError()
        time.sleep(2.0)
        free = _vram_free_mb()
        if free >= SONG_MIN_FREE_MB:
            logger.info(f"[{tid}] 腾出显存，现空闲 {free}MB")
            return
    raise RuntimeError(
        f"显存不足（空闲 {free}MB，需约 {SONG_MIN_FREE_MB}MB）：显卡正被占用。"
        f"请在运维看板点「释放显存」后重试，或等下播时段自动空闲")


def run_separate(song, task: dict, num_overlap: int = 0, model_kind: str = "bs"):
    """song: float32 [C, T] @44100 → (vocals [C,T], accomp [C,T])，伴奏=原曲-人声（相位一致反相）。
    Song-P2/O2: num_overlap>0 时覆盖配置的重叠推理次数（TTA 平均，和声重的歌人声更干净）。
    Song-P3/O2: model_kind=mel 时用 Kim Mel-Band RoFormer（精细档专用人声模型）。"""
    import numpy as np
    import torch
    from utils.model_utils import demix
    from utils.audio_utils import normalize_audio, denormalize_audio
    model_type = _SEP_KINDS.get(model_kind, _SEP_KINDS["bs"])[0]
    model, config = _load_sep(model_kind)
    if task.get("cancel"):
        raise CancelledError()
    mix = song if song.ndim == 2 else np.stack([song, song])
    if mix.shape[0] == 1:
        mix = np.concatenate([mix, mix], axis=0)
    mix_orig = mix.copy()
    norm_params = None
    if config.inference.get("normalize", False):
        mix, norm_params = normalize_audio(mix)
    old_overlap = config.inference.num_overlap
    if num_overlap > 0:
        config.inference.num_overlap = num_overlap
    try:
        with torch.no_grad():
            est = demix(config, model, mix, _device(), model_type=model_type, pbar=False)
    finally:
        config.inference.num_overlap = old_overlap
    vocals = est["vocals"] if isinstance(est, dict) else est
    if norm_params is not None:
        vocals = denormalize_audio(vocals, norm_params)
    vocals = np.asarray(vocals, dtype=np.float32)
    accomp = (mix_orig - vocals).astype(np.float32)
    return vocals, accomp


def run_svc(source_mono, ref_mono, task: dict, diffusion_steps: int = 30,
            pitch_shift: Optional[int] = None, progress=None):
    """干声 [T]@44100 + 参考 [T]@44100 → 转换后 [T]@44100（整体 no_grad，
    对齐原 my_inference.run_inference 的 @torch.no_grad()——rmvpe 产出 inference
    tensor，缺这层会在 length_regulator 触发 autograd 报错）。"""
    import torch
    with torch.no_grad():
        return _run_svc_impl(source_mono, ref_mono, task, diffusion_steps,
                             pitch_shift, progress)


def _run_svc_impl(source_mono, ref_mono, task: dict, diffusion_steps: int = 30,
                  pitch_shift: Optional[int] = None, progress=None):
    """干声 [T]@44100 + 参考 [T]@44100 → 转换后 [T]@44100。
    改造自 my_inference.run_inference：本地权重 / 分块可取消 / 进度回调。"""
    import numpy as np
    import torch
    import torchaudio
    from mm4 import preprocess_voice_conversion

    b = _load_svc()
    device = _device()
    model = b["model"]
    sr = 44100
    hop_length = 512
    max_context_window = sr // hop_length * 30
    overlap_frame_len = 16
    overlap_wave_len = overlap_frame_len * hop_length

    source_audio = torch.tensor(source_mono).unsqueeze(0).float().to(device)
    ref_audio = torch.tensor(ref_mono[: sr * 25]).unsqueeze(0).float().to(device)

    def _semantic(waves_16k):
        fe = b["feat_extractor"]
        wm = b["whisper"]
        inputs = fe([waves_16k.squeeze(0).cpu().numpy()], return_tensors="pt",
                    return_attention_mask=True, sampling_rate=16000)
        feats = wm._mask_input_features(
            inputs.input_features, attention_mask=inputs.attention_mask).to(device)
        with torch.no_grad():
            out = wm.encoder(feats.to(wm.encoder.dtype), head_mask=None,
                             output_attentions=False, output_hidden_states=False,
                             return_dict=True)
        S = out.last_hidden_state.to(torch.float32)
        return S[:, : waves_16k.size(-1) // 320 + 1]

    conv16k = torchaudio.functional.resample(source_audio, sr, 16000)
    if conv16k.size(-1) <= 16000 * 30:
        S_alt = _semantic(conv16k)
    else:                                   # >30s 分窗重叠推进（照搬原实现）
        overlap_s, S_list, buf, done_t = 5, [], None, 0
        while done_t < conv16k.size(-1):
            if buf is None:
                chunk = conv16k[:, done_t: done_t + 16000 * 30]
            else:
                chunk = torch.cat([buf, conv16k[:, done_t: done_t + 16000 * (30 - overlap_s)]], dim=-1)
            S_chunk = _semantic(chunk)
            S_list.append(S_chunk if done_t == 0 else S_chunk[:, 50 * overlap_s:])
            buf = chunk[:, -16000 * overlap_s:]
            done_t += 30 * 16000 if done_t == 0 else chunk.size(-1) - 16000 * overlap_s
            if task.get("cancel"):
                raise CancelledError()
        S_alt = torch.cat(S_list, dim=1)

    ref16k = torchaudio.functional.resample(ref_audio, sr, 16000)
    S_ori = _semantic(ref16k)

    mel = b["mel_fn"](source_audio.float())
    mel2 = b["mel_fn"](ref_audio.float())
    target_lengths = torch.LongTensor([int(mel.size(2))]).to(mel.device)
    target2_lengths = torch.LongTensor([mel2.size(2)]).to(mel2.device)

    feat2 = torchaudio.compliance.kaldi.fbank(
        ref16k, num_mel_bins=80, dither=0, sample_frequency=16000)
    feat2 = feat2 - feat2.mean(dim=0, keepdim=True)
    style2 = b["campplus"](feat2.unsqueeze(0))

    # F0 提取 + 自动/手动升降调
    F0_ori = b["f0_fn"](ref16k[0], thred=0.03)
    F0_alt = b["f0_fn"](conv16k[0], thred=0.03)
    F0_ori = torch.from_numpy(F0_ori).to(device)[None]
    F0_alt = torch.from_numpy(F0_alt).to(device)[None]
    voiced_ori = F0_ori[F0_ori > 1]
    voiced_alt = F0_alt[F0_alt > 1]
    shifted_f0_alt = torch.exp(torch.log(F0_alt + 1e-5).clone())
    shifted_f0_alt, used_shift = preprocess_voice_conversion(
        voiced_f0_ori=voiced_ori, voiced_f0_alt=voiced_alt,
        shifted_f0_alt=shifted_f0_alt, enable_adaptive=True,
        max_shift_semitones=24, forch_pitch_shift=pitch_shift)
    logger.info(f"pitch shift = {used_shift} semitones "
                f"({'手动' if pitch_shift is not None else '自动'})")

    if task.get("cancel"):
        raise CancelledError()

    cond, _, _, _, _, style_cond = model.length_regulator(
        S_alt, ylens=target_lengths, n_quantizers=3, f0=shifted_f0_alt,
        style=style2, return_style_residual=True)
    prompt_condition, _, _, _, _, style_prompt = model.length_regulator(
        S_ori, ylens=target2_lengths, n_quantizers=3, f0=F0_ori,
        style=style2, return_style_residual=True)

    max_source_window = max_context_window - mel2.size(2)
    processed = 0
    chunks = []
    prev_chunk = None
    total_frames = cond.size(1)
    while processed < total_frames:
        if task.get("cancel"):
            raise CancelledError()
        chunk_cond = cond[:, processed: processed + max_source_window]
        is_last = processed + max_source_window >= total_frames
        cat_condition = torch.cat([prompt_condition, chunk_cond], dim=1)
        if b["use_style_residual"]:
            cat_style = torch.cat(
                [style_prompt, style_cond[:, processed: processed + max_source_window]], dim=1)
        else:
            cat_style = None
        with torch.autocast(device_type=device.type, dtype=torch.float16):
            vc_target = model.cfm.inference(
                cat_condition,
                torch.LongTensor([cat_condition.size(1)]).to(mel2.device),
                mel2, style2, None, diffusion_steps,
                inference_cfg_rate=0.7, style_r=cat_style)
            vc_target = vc_target[:, :, mel2.size(-1):]
        vc_wave = b["vocoder"](vc_target.float()).squeeze()[None, :]

        def _crossfade(c1, c2, ov):
            f_out = np.cos(np.linspace(0, np.pi / 2, ov)) ** 2
            f_in = np.cos(np.linspace(np.pi / 2, 0, ov)) ** 2
            if len(c2) < ov:
                c2[:ov] = c2[:ov] * f_in[: len(c2)] + (c1[-ov:] * f_out)[: len(c2)]
            else:
                c2[:ov] = c2[:ov] * f_in + c1[-ov:] * f_out
            return c2

        if processed == 0 and is_last:
            chunks.append(vc_wave[0].cpu().numpy())
            break
        if processed == 0:
            chunks.append(vc_wave[0, :-overlap_wave_len].cpu().numpy())
            prev_chunk = vc_wave[0, -overlap_wave_len:]
        elif is_last:
            chunks.append(_crossfade(prev_chunk.cpu().numpy(),
                                     vc_wave[0].cpu().numpy(), overlap_wave_len))
        else:
            chunks.append(_crossfade(prev_chunk.cpu().numpy(),
                                     vc_wave[0, :-overlap_wave_len].cpu().numpy(),
                                     overlap_wave_len))
            prev_chunk = vc_wave[0, -overlap_wave_len:]
        processed += vc_target.size(2) - overlap_frame_len
        if progress:
            progress(min(1.0, processed / max(1, total_frames)))
        if is_last:
            break

    out = np.concatenate(chunks).astype(np.float32)
    return out, int(used_shift)


def run_mix(vocals_mono, accomp, sr: int = 44100,
            vocal_gain_db: float = 0.0, accomp_gain_db: float = 0.0):
    """转换后人声 [T] + 伴奏 [C,T] → 成品 [2,T]。对齐→增益→软限幅。
    （不用 torchaudio.sox_effects：Windows 无 sox 后端；混响留待后续打磨）"""
    import numpy as np
    v = np.asarray(vocals_mono, dtype=np.float32)
    a = np.asarray(accomp, dtype=np.float32)
    if a.ndim == 1:
        a = np.stack([a, a])
    T = max(v.shape[-1], a.shape[-1])
    if v.shape[-1] < T:
        v = np.pad(v, (0, T - v.shape[-1]))
    if a.shape[-1] < T:
        a = np.pad(a, ((0, 0), (0, T - a.shape[-1])))
    v = v * (10 ** (vocal_gain_db / 20.0))
    a = a * (10 ** (accomp_gain_db / 20.0))
    mix = a + np.stack([v, v])
    peak = float(np.abs(mix).max() or 1.0)
    if peak > 0.985:
        mix = np.tanh(mix / peak * 1.2) * 0.92 / np.tanh(1.2)   # 软限幅防爆音
    return mix.astype(np.float32)


# ─────────────────────────── 任务队列 ───────────────────────────

TASKS: dict = {}
_task_q: "queue.Queue[str]" = queue.Queue()
TMP_ROOT = BASE / "temp" / "song_studio"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

# Song-P2/O1: 预分离缓存——同一首歌（按内容哈希）只分离一次。
# 换角色重唱/调整调门重跑时直接命中，热路径砍掉 ~15s 的分离段。
SEP_CACHE = TMP_ROOT / "sep_cache"
SEP_CACHE.mkdir(parents=True, exist_ok=True)
SEP_CACHE_GB = float(os.environ.get("SONG_SEP_CACHE_GB", "10"))


def _sep_cache_get(song_hash: str):
    """命中返回 (vocals[C,T], accomp[C,T])，未命中返回 None。命中即刷新 mtime（LRU）。"""
    import numpy as np
    import soundfile as sf
    d = SEP_CACHE / song_hash
    fv, fa = d / "vocals.wav", d / "accomp.wav"
    if not (fv.exists() and fa.exists()):
        return None
    try:
        v, _ = sf.read(fv, dtype="float32", always_2d=True)
        a, _ = sf.read(fa, dtype="float32", always_2d=True)
        now = time.time()
        os.utime(d, (now, now))
        return v.T, a.T
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        return None


def _sep_cache_put(song_hash: str, vocals, accomp):
    """stems 落盘（PCM16，4 分钟歌 ~80MB）+ 总量超限时按 mtime 逐出最旧。"""
    import soundfile as sf
    d = SEP_CACHE / song_hash
    try:
        d.mkdir(parents=True, exist_ok=True)
        sf.write(d / "vocals.wav", vocals.T, 44100, format="WAV", subtype="PCM_16")
        sf.write(d / "accomp.wav", accomp.T, 44100, format="WAV", subtype="PCM_16")
    except Exception as e:
        logger.warning(f"分离缓存写入失败(不影响任务): {e}")
        shutil.rmtree(d, ignore_errors=True)
        return
    try:                                     # LRU 逐出
        dirs = [p for p in SEP_CACHE.iterdir() if p.is_dir()]
        total = sum(f.stat().st_size for p in dirs for f in p.glob("*.wav"))
        if total > SEP_CACHE_GB * 1024**3:
            for p in sorted(dirs, key=lambda x: x.stat().st_mtime):
                if total <= SEP_CACHE_GB * 1024**3 * 0.9:
                    break
                sz = sum(f.stat().st_size for f in p.glob("*.wav"))
                shutil.rmtree(p, ignore_errors=True)
                total -= sz
    except Exception:
        pass


def _sep_cache_stats() -> dict:
    try:
        dirs = [p for p in SEP_CACHE.iterdir() if p.is_dir()]
        total = sum(f.stat().st_size for p in dirs for f in p.glob("*.wav"))
        return {"entries": len(dirs), "bytes": total}
    except Exception:
        return {"entries": 0, "bytes": 0}


def _task_dir(tid: str) -> Path:
    d = TMP_ROOT / tid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _set(tid: str, **kw):
    t = TASKS.get(tid)
    if t is not None:
        t.update(kw)


def _cleanup_old():
    now = time.time()
    for tid, t in list(TASKS.items()):
        if now - t.get("created", now) > TASK_TTL_SEC:
            shutil.rmtree(TMP_ROOT / tid, ignore_errors=True)
            TASKS.pop(tid, None)


def _worker():
    import numpy as np
    while True:
        tid = _task_q.get()
        t = TASKS.get(tid)
        if t is None or t.get("cancel"):
            _set(tid, status="cancelled")
            continue
        d = _task_dir(tid)
        t0 = time.time()
        timings = {}
        try:
            song_bytes = (d / "song.bin").read_bytes()
            song, _ = load_audio_any(song_bytes, 44100, mono=False)
            if song.ndim == 1:
                song = np.stack([song, song])
            dur = song.shape[-1] / 44100.0
            if dur > MAX_SONG_SEC:
                raise ValueError(f"歌曲 {dur:.0f}s 超过 {MAX_SONG_SEC}s 上限，请截选后再试")
            ref, _ = load_audio_any((d / "ref.bin").read_bytes(), 44100, mono=True)

            # P3/O6: 开工前直播让路（挂起时 GPU 一点不碰；直播结束自动继续）
            timings["yield_ms"] = _yield_wait(tid, t)
            # P4: 不直播但闲置引擎占卡 → 请 Hub 腾显存，不够则人话拒（绝不裸 OOM）
            _ensure_vram(tid, t)

            # ① 人声分离（Song-P2/O1: 内容哈希缓存——换角色/换调门重唱同一首歌免二次分离）
            sep_overlap = int(t.get("sep_overlap", 0))
            # P3/O2: 精细档分离模型解析——mel 缺权重自动回退 BS（如实记录实际用了谁）
            sep_kind = "bs"
            if t.get("sep_model") == "mel":
                if _cap().get("separate_mel"):
                    sep_kind = "mel"
                else:
                    logger.info(f"[{tid}] Mel-Band 权重缺失，精细档分离回退 BS-RoFormer")
            t["sep_model_used"] = sep_kind
            song_hash = (hashlib.sha1(song_bytes).hexdigest()[:20]
                         + f"_ov{sep_overlap or 2}"
                         + ("_mel" if sep_kind == "mel" else ""))
            del song_bytes
            if t.get("skip_separation"):
                vocals_mono = song.mean(axis=0)
                accomp = np.zeros_like(song)
                timings["separate_ms"] = 0
            else:
                cached = _sep_cache_get(song_hash)
                if cached is not None:
                    vocals, accomp = cached
                    vocals_mono = vocals.mean(axis=0)
                    timings["separate_ms"] = 0
                    t["sep_cache_hit"] = True
                    logger.info(f"[{tid}] 分离缓存命中 {song_hash}")
                else:
                    _set(tid, status="separating", progress=8,
                         detail="正在把人声从伴奏里拆出来")
                    ts = time.time()
                    vocals, accomp = run_separate(song, t, num_overlap=sep_overlap,
                                                  model_kind=sep_kind)
                    vocals_mono = vocals.mean(axis=0)
                    timings["separate_ms"] = int((time.time() - ts) * 1000)
                    _sep_cache_put(song_hash, vocals, accomp)
                    if not KEEP_LOADED:
                        _unload("sep")
            _set(tid, progress=40)
            if t.get("cancel"):
                raise CancelledError()

            # P3/O6: 分离期间若开播了，SVC 重活同样让路
            timings["yield_ms"] += _yield_wait(tid, t)

            # ② 音色转换
            _set(tid, status="converting", progress=42,
                 detail="正在用目标音色重唱")
            ts = time.time()

            def _prog(frac):
                _set(tid, progress=42 + int(frac * 46))

            converted, used_shift = run_svc(
                vocals_mono, ref, t,
                diffusion_steps=t.get("diffusion_steps", 30),
                pitch_shift=t.get("pitch_shift"), progress=_prog)
            timings["convert_ms"] = int((time.time() - ts) * 1000)
            if t.get("cancel"):
                raise CancelledError()

            # ③ 混音出品
            _set(tid, status="mixing", progress=90, detail="人声和伴奏正在合成成品")
            ts = time.time()
            mix = run_mix(converted, accomp,
                          vocal_gain_db=t.get("vocal_gain_db", 0.0),
                          accomp_gain_db=t.get("accomp_gain_db", 0.0))
            timings["mix_ms"] = int((time.time() - ts) * 1000)

            (d / "result.wav").write_bytes(_wav_bytes(mix, 44100))
            (d / "vocals_converted.wav").write_bytes(_wav_bytes(converted, 44100))
            elapsed = int((time.time() - t0) * 1000)
            _set(tid, status="done", progress=100, detail="",
                 result={
                     "duration_s": round(dur, 1),
                     "pitch_shift": used_shift,
                     "timings": timings,
                     "elapsed_ms": elapsed,
                     "rtf": round(elapsed / 1000.0 / max(0.1, dur), 2),
                     "sep_cache_hit": bool(t.get("sep_cache_hit")),
                     "sep_model_used": t.get("sep_model_used", "bs"),   # P3/O2
                 })
            logger.info(f"[{tid}] done {dur:.0f}s歌曲 用时{elapsed/1000:.0f}s "
                        f"(sep {timings.get('separate_ms',0)}ms / "
                        f"svc {timings.get('convert_ms',0)}ms)")
        except CancelledError:
            _set(tid, status="cancelled", detail="已取消")
            logger.info(f"[{tid}] cancelled")
        except Exception as e:
            logger.exception(f"[{tid}] failed")
            _set(tid, status="error", detail=f"{type(e).__name__}: {e}")
        finally:
            if not KEEP_LOADED:
                _unload("all")
            for f in ("song.bin", "ref.bin"):
                try:
                    (d / f).unlink(missing_ok=True)
                except Exception:
                    pass
            _cleanup_old()


threading.Thread(target=_worker, daemon=True, name="song_worker").start()


# ─────────────────────────── FastAPI ───────────────────────────

from fastapi import FastAPI, HTTPException          # noqa: E402
from fastapi.responses import FileResponse          # noqa: E402
from pydantic import BaseModel                      # noqa: E402

app = FastAPI(title="Song Studio (YingMusic-SVC)", version="1.0")
try:
    import service_auth
    service_auth.secure(app, name="song_studio")
except Exception as _e:
    logger.warning(f"service_auth 未接入: {_e}")


class CoverRequest(BaseModel):
    song_b64:        str
    reference_b64:   str
    song_name:       str = ""
    pitch_shift:     Optional[int] = None    # None=自动
    diffusion_steps: int = 30                # 30 标准 / 50 精细
    vocal_gain_db:   float = 0.0
    accomp_gain_db:  float = 0.0
    skip_separation: bool = False            # 干声直转（用户已有清唱）
    sep_overlap:     int = 0                 # P2/O2: 0=配置默认(2)；精细档传 4（TTA 更干净）
    sep_model:       str = "auto"            # P3/O2: auto=BS | mel=Mel-Band(缺权重自动回退 BS)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "song_studio",
        "engine": "yingmusic_svc",
        "capabilities": _cap(),
        "models_present": {k: v for k, v in _weights_ok().items()},
        "loaded": {"separator": bool(_sep_bundles),
                   "sep_kinds": list(_sep_bundles.keys()),
                   "svc": _svc_bundle is not None},
        "live_yield": dict(_YIELD_LAST),      # P3/O6: 最近一次直播让路探测结果

        "queue": _task_q.qsize(),
        "active": [tid for tid, t in TASKS.items()
                   if t.get("status") in ("queued", "separating", "converting", "mixing")],
        "sep_cache": _sep_cache_stats(),      # P2/O1: 预分离缓存水位
    }


@app.post("/v1/cover")
def submit_cover(req: CoverRequest):
    caps = _cap()
    if not (caps["svc"] if req.skip_separation else caps["cover"]):
        raise HTTPException(503, "翻唱引擎权重未就绪，请先运行 tools/setup_song_studio.py --all")
    try:
        song = base64.b64decode(req.song_b64)
        ref = base64.b64decode(req.reference_b64)
    except Exception:
        raise HTTPException(400, "base64 解码失败")
    if len(song) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"歌曲文件超过 {MAX_UPLOAD_MB}MB 上限")
    if len(ref) > 12 * 1024 * 1024:
        raise HTTPException(400, "参考音频过大（上限 12MB），请截取 3~30 秒")
    if not song or not ref:
        raise HTTPException(400, "歌曲与参考音色都不能为空")

    tid = uuid.uuid4().hex[:12]
    d = _task_dir(tid)
    (d / "song.bin").write_bytes(song)
    (d / "ref.bin").write_bytes(ref)
    TASKS[tid] = {
        "status": "queued", "progress": 2, "detail": "排队中",
        "created": time.time(), "cancel": False,
        "song_name": req.song_name or "未命名歌曲",
        "pitch_shift": req.pitch_shift,
        "diffusion_steps": max(10, min(100, req.diffusion_steps)),
        "vocal_gain_db": max(-12.0, min(12.0, req.vocal_gain_db)),
        "accomp_gain_db": max(-12.0, min(12.0, req.accomp_gain_db)),
        "skip_separation": req.skip_separation,
        "sep_overlap": max(0, min(8, req.sep_overlap)),
        "sep_model": req.sep_model if req.sep_model in ("auto", "bs", "mel") else "auto",
    }
    _task_q.put(tid)
    return {"ok": True, "task_id": tid,
            "queue_pos": _task_q.qsize()}


def _queue_pos(tid: str) -> int:
    """P2/O5: 排队位次——排在本任务前面、还没轮到的任务数（0=下一个就是它）。"""
    t = TASKS.get(tid)
    if t is None or t.get("status") != "queued":
        return 0
    return sum(1 for x in TASKS.values()
               if x.get("status") == "queued" and x.get("created", 0) < t.get("created", 0))


@app.get("/v1/task/{tid}")
def task_status(tid: str):
    t = TASKS.get(tid)
    if t is None:
        raise HTTPException(404, "任务不存在或已过期")
    out = {"task_id": tid, "status": t["status"], "progress": t.get("progress", 0),
           "detail": t.get("detail", ""), "song_name": t.get("song_name", ""),
           "result": t.get("result"),
           "elapsed_ms": int((time.time() - t["created"]) * 1000)}
    if t["status"] == "queued":
        pos = _queue_pos(tid)
        busy = any(x.get("status") in ("separating", "converting", "mixing")
                   for x in TASKS.values())
        out["queue_ahead"] = pos + (1 if busy else 0)
        if out["queue_ahead"]:
            out["detail"] = f"排队中：前面还有 {out['queue_ahead']} 首"
    return out


@app.get("/v1/task/{tid}/audio")
def task_audio(tid: str, stem: str = "result"):
    t = TASKS.get(tid)
    if t is None:
        raise HTTPException(404, "任务不存在或已过期")
    if t.get("status") != "done":
        raise HTTPException(400, f"任务未完成: {t.get('status')}")
    name = {"result": "result.wav", "vocals": "vocals_converted.wav"}.get(stem)
    if not name:
        raise HTTPException(400, "stem 仅支持 result / vocals")
    p = TMP_ROOT / tid / name
    if not p.exists():
        raise HTTPException(404, "产物文件已清理")
    return FileResponse(str(p), media_type="audio/wav", filename=f"cover_{tid}.wav")


@app.post("/v1/task/{tid}/cancel")
def task_cancel(tid: str):
    t = TASKS.get(tid)
    if t is None:
        raise HTTPException(404, "任务不存在或已过期")
    t["cancel"] = True
    if t.get("status") == "queued":
        _set(tid, status="cancelled", detail="已取消")
    return {"ok": True, "task_id": tid}


if __name__ == "__main__":
    import uvicorn
    caps = _cap()
    logger.info(f"Song Studio 启动 :{PORT} capabilities={caps}")
    if not caps["cover"]:
        missing = [k for k, v in _weights_ok().items() if not v]
        logger.warning(f"权重缺失 {missing} → 翻唱能力关闭（/health 如实上报），"
                       f"补齐: python tools/setup_song_studio.py --all")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
