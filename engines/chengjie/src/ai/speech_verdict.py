"""预览产物「有声/无声」服务端终审（#121 三进宫除根，2026-09-02 KKXSTU 实锤）。

三轮误报史：
  1. 0831：前端 WebAudio 单信号「峰值 < 0.004」咬正常短英句；
  2. 0901：改双信号（峰值 < 0.002 且 RMS < 0.0008）仍在 1.0.66/1.0.70 复发——
     KKXSTU 包里 ``tts-20260902-202949-c0592a3d.wav`` 被 Whisper **全文转录成功**
     （"what are you up to right now you haven't eaten yet…"），红条照亮。
  3. 本轮：不再让浏览器端的解码启发式当终审。服务端手里握着两份硬证据——
     ① 预览链本来就跑了合成回验（synth_verify：ASR 把产物转回文字并与送稿比
        CER），**能转出文字且与送稿相关＝音频有声**；
     ② 最终落盘字节的 PCM 能量（avatar_voice.detect_silent_audio：wav 走 stdlib
        零依赖，压缩容器走 ffmpeg，判不了返 None）。
     两者合成一个权威裁决随 tts-test 响应下发（``voice_meta.speech``）：
       voiced  → 前端**绝不**亮红条（客户端解码启发式只画波形不裁决）；
       silent  → 服务端确认无声，前端直接亮红条+禁发（真空壳照拦）；
       unknown → 服务端拿不到证据（转写器未接、非 16bit wav 且无 ffmpeg），
                 前端才用自己的启发式兜底（并加解码合理性闸）。

纯函数 ``speech_verdict`` 可单测；``judge_preview_speech`` 只多一步读文件。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: 转写命中至少几个内容字符才算「听出了字」（防 ASR 对静音幻觉出一两个字）
TRANSCRIPT_MIN_CHARS = 4
#: 转写内容与送稿的字错率上限：CER 高于此值＝转出来的字与送稿无关（幻觉/杂音），
#: 不能当有声证据。KKXSTU 实锤 CER≈0；synth_verify 自身阈值 0.30/0.35。
TRANSCRIPT_MAX_CER = 0.75


def speech_verdict(
    synth_verify: Optional[Dict[str, Any]],
    energy: Optional[bool],
) -> Dict[str, Any]:
    """合成两份证据 → ``{"speech", "basis", "transcript_chars", "energy"}``。

    ``synth_verify``：tts_pipeline.verify_and_retry_synth 的返回（含 cer/hyp_chars），
    None＝本次没跑回验；``energy``：detect_silent_audio 的返回
    （True=确定无声 / False=有能量 / None=判不了）。

    规则（金标：「ASR 能从产物转出与送稿相关的文字」即有声，终审白名单）：
      转写命中且 CER 相关     → voiced（basis=transcript；能量 False 附 +energy；
                               能量 True 与之矛盾时仍判 voiced 但 basis 带 conflict
                               并打 WARNING——静音上幻觉出与送稿相关的整句在
                               min_chars 门槛下不可能，字节级探测器更可能读错头）
      energy True            → silent（服务端确认无声：真空壳照拦）
      energy False           → voiced（basis=energy：有能量、无转写证据）
      其余                    → unknown
    """
    sv = synth_verify if isinstance(synth_verify, dict) else {}
    try:
        hyp_chars = int(sv.get("hyp_chars") or 0)
    except (TypeError, ValueError):
        hyp_chars = 0
    try:
        cer = float(sv.get("cer")) if sv.get("cer") is not None else -1.0
    except (TypeError, ValueError):
        cer = -1.0
    transcript_ok = hyp_chars >= TRANSCRIPT_MIN_CHARS and 0.0 <= cer <= TRANSCRIPT_MAX_CER
    energy_tag = "unknown" if energy is None else ("silent" if energy else "ok")

    if transcript_ok:
        speech = "voiced"
        if energy is False:
            basis = "transcript+energy"
        elif energy is True:
            basis = "transcript(conflict:energy)"
            logger.warning(
                "[speech_verdict] 转写命中（%d 字, CER=%.2f）但能量探测判无声——"
                "以转写为准判有声，能量探测器疑似误读容器", hyp_chars, cer)
        else:
            basis = "transcript"
    elif energy is True:
        speech, basis = "silent", "energy"
    elif energy is False:
        speech, basis = "voiced", "energy"
    else:
        speech, basis = "unknown", ""
    return {
        "speech": speech,
        "basis": basis,
        "transcript_chars": hyp_chars,
        "energy": energy_tag,
    }


def judge_preview_speech(
    audio_path: Any,
    audio_format: str,
    synth_verify: Optional[Dict[str, Any]],
    *,
    max_bytes: int = 24 * 1024 * 1024,
) -> Dict[str, Any]:
    """对**最终落盘**的预览文件做能量检测，再与转写证据合成裁决。任何异常 → 按
    「能量判不了」处理（绝不因为终审本身出错而阻塞试听）。"""
    energy: Optional[bool] = None
    try:
        p = Path(str(audio_path or ""))
        if p.is_file() and 0 < p.stat().st_size <= max_bytes:
            from src.ai.avatar_voice import detect_silent_audio
            energy = detect_silent_audio(p.read_bytes(), str(audio_format or ""))
    except Exception:
        logger.debug("[speech_verdict] 能量检测异常（按判不了处理）", exc_info=True)
        energy = None
    return speech_verdict(synth_verify, energy)
