"""人设语音三态（L-2 #205，D-L4，2026-09-06）——纯函数核心，零 IO 依赖（文件存在性
检查可关）。

事故（8CE7U3 / 5QMZ5E / ERS5QQ，skuio）：语音 tab 是「语音 BACKEND 五选一 + 手填
VOICE ID」的研发面直出；Mizuki 存成 ``avatar_clone + ja-JP-NanamiNeural``（克隆引擎
+ 预置声名，没有录音）保存成功、页面还标「已绑定克隆音色」，合成日志
``CER=0.719 应克隆未克隆``。

三态（``voice_profile.voice_mode``）：
- ``clone``  用我上传的录音（克隆声）——引擎由系统定（登记来源推 avatar_clone /
  voice_clone_lan / voice_clone_command），必须有 ``reference_audio_path`` 且文件在；
- ``preset`` 用预置声——引擎由系统定（edge_tts；存量 openai/elevenlabs 照旧），
  ``voice`` 必须在该引擎的合法音色表（edge_voice_catalog / OPENAI_VOICES）；
- ``off``    不发语音——**新建人设默认**（D-L4）；TTSPipeline 据此直接判「改发文字」。

存量档没有 ``voice_mode`` → ``derive_voice_mode`` 按现状**只读推导**（有克隆录音→
clone；其余→preset 并标注 basis），绝不写回（三态迁移全案是 P3，本条只做闸门）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from src.ai.voice_profile_guard import (
    CLONE_BACKENDS,
    infer_clone_backend,
    is_registered_clone_profile,
)

VOICE_MODE_CLONE = "clone"
VOICE_MODE_PRESET = "preset"
VOICE_MODE_OFF = "off"
VOICE_MODES = (VOICE_MODE_CLONE, VOICE_MODE_PRESET, VOICE_MODE_OFF)

#: 预置声引擎（非克隆）。edge 是系统给用户的缺省预置引擎；openai / elevenlabs
#: 只承接存量配置（选择器不新增）。
PRESET_BACKENDS = ("edge_tts", "openai", "elevenlabs")

#: 新建人设的缺省 voice_profile（D-L4：默认不发语音）
DEFAULT_NEW_VOICE_PROFILE: Dict[str, Any] = {"voice_mode": VOICE_MODE_OFF}


def _s(v: Any) -> str:
    return str(v or "").strip()


def _looks_like_preset_voice_name(voice: str) -> bool:
    """``ja-JP-NanamiNeural`` / ``alloy`` 这类预置声名（克隆态填了它＝非法组合）。"""
    v = _s(voice)
    if not v:
        return False
    if v.endswith("Neural"):
        return True
    try:
        from src.ai.edge_voice_catalog import OPENAI_VOICES, is_known_edge_voice
        return v.lower() in OPENAI_VOICES or is_known_edge_voice(v)
    except Exception:
        return False


#: 对外别名（voice_enroll.copy_voice_profile 复用收口用）
looks_like_preset_voice_name = _looks_like_preset_voice_name


def derive_voice_mode(vp: Any) -> Tuple[str, str]:
    """voice_profile → ``(mode, basis)``。

    basis：``explicit``（显式 voice_mode）/ ``registered_clone``（登记齐备的克隆档）/
    ``clone_backend``（克隆引擎但登记不齐——Mizuki 形态，需修正）/ ``preset_backend``
    / ``preset_voice``（只填了预置声名）/ ``inherit_global``（什么都没填＝沿用全局，
    D-L4 归预置态并标注）/ ``disabled``（enabled 显式 False）。
    """
    if not isinstance(vp, dict) or not vp:
        return VOICE_MODE_PRESET, "inherit_global"
    mode = _s(vp.get("voice_mode")).lower()
    if mode in VOICE_MODES:
        return mode, "explicit"
    if vp.get("enabled") is False and not _s(vp.get("backend")) and not _s(vp.get("voice")):
        return VOICE_MODE_OFF, "disabled"
    if is_registered_clone_profile(vp):
        return VOICE_MODE_CLONE, "registered_clone"
    backend = _s(vp.get("backend")).lower() or infer_clone_backend(vp)
    if backend in CLONE_BACKENDS:
        return VOICE_MODE_CLONE, "clone_backend"
    if backend in PRESET_BACKENDS:
        return VOICE_MODE_PRESET, "preset_backend"
    if _s(vp.get("voice")):
        return VOICE_MODE_PRESET, "preset_voice"
    return VOICE_MODE_PRESET, "inherit_global"


def resolve_clone_backend(vp: Any) -> str:
    """克隆态该用哪个引擎：现有克隆 backend > 登记来源推断 > avatar_clone（本机 7852 主力）。"""
    if not isinstance(vp, dict):
        return "avatar_clone"
    b = _s(vp.get("backend")).lower()
    if b in CLONE_BACKENDS:
        return b
    return infer_clone_backend(vp) or "avatar_clone"


def validate_voice_profile(
    vp: Any, *, check_files: bool = True, base_dir: Optional[str] = None,
) -> List[Dict[str, str]]:
    """三态保存校验 → 问题清单 ``[{code, detail, severity}]``（空＝合法）。

    ``severity``：``error``（拒绝保存）/ ``warning``（可保存，回传给前端黄字提示）。
    只在人设**自己**的 voice_profile 上判（不看全局层）：
    - clone：必须有录音身份（``reference_audio_path`` 或 hub ``speaker_id``；两者
      皆无＝Mizuki 形态 → error）；填了路径但本机文件不在 → **warning**（托管 /
      hub 登记的档参考音可能只在远端，``check_files`` 可关）；``voice`` 若是预置声名
      （*Neural / openai 名）→ error ``clone_with_preset_voice``；
    - preset：backend 不得是克隆引擎；显式 preset 必须选了音色；音色必须在该引擎
      合法表（edge 查目录 + 白形；openai 查表；elevenlabs 非空即可）；
      存量 ``inherit_global``（没填任何东西）不报错——D-L4「其余→预置声并标注」；
    - off：恒合法；
    - voice_mode 填了三态以外的值 → ``invalid_mode``。
    """
    problems: List[Dict[str, str]] = []
    if not isinstance(vp, dict) or not vp:
        return problems

    def _err(code: str, detail: str = "") -> None:
        problems.append({"code": code, "detail": detail, "severity": "error"})

    def _warn(code: str, detail: str = "") -> None:
        problems.append({"code": code, "detail": detail, "severity": "warning"})

    raw_mode = _s(vp.get("voice_mode")).lower()
    if raw_mode and raw_mode not in VOICE_MODES:
        _err("invalid_mode", raw_mode)
        return problems
    mode, basis = derive_voice_mode(vp)
    backend = _s(vp.get("backend")).lower()
    voice = _s(vp.get("voice"))
    if mode == VOICE_MODE_OFF:
        return problems
    if mode == VOICE_MODE_CLONE:
        ref = _s(vp.get("reference_audio_path"))
        spk = _s(vp.get("speaker_id"))
        has_ref = bool(ref) and any(ch.isalnum() for ch in ref)
        has_spk = bool(spk) and any(ch.isalnum() for ch in spk)
        if not has_ref and not has_spk:
            _err("clone_missing_reference")
        elif has_ref and check_files:
            path = ref if os.path.isabs(ref) or not base_dir else os.path.join(base_dir, ref)
            if not os.path.isfile(path):
                _warn("clone_reference_file_missing", ref)
        if voice and _looks_like_preset_voice_name(voice):
            _err("clone_with_preset_voice", voice)
        if backend and backend not in CLONE_BACKENDS:
            _err("clone_backend_not_clone", backend)
        return problems
    # preset
    if backend in CLONE_BACKENDS:
        _err("preset_backend_is_clone", backend)
        return problems
    if not voice:
        if basis == "explicit":
            _err("preset_voice_missing")
        return problems
    engine = backend or "edge_tts"
    if engine == "edge_tts":
        try:
            from src.ai.edge_voice_catalog import is_known_edge_voice
            from src.ai.tts_pipeline import _edge_voice_wellformed
            ok = is_known_edge_voice(voice) or _edge_voice_wellformed(voice)
        except Exception:
            ok = voice.endswith("Neural")
        if not ok:
            _err("preset_voice_unknown", f"edge_tts:{voice}")
    elif engine == "openai":
        try:
            from src.ai.edge_voice_catalog import OPENAI_VOICES
            ok = voice.lower() in OPENAI_VOICES
        except Exception:
            ok = True
        if not ok:
            _err("preset_voice_unknown", f"openai:{voice}")
    return problems


def split_problems(problems: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """→ ``(errors, warnings)``。"""
    errs = [p for p in problems or [] if (p or {}).get("severity", "error") != "warning"]
    warns = [p for p in problems or [] if (p or {}).get("severity") == "warning"]
    return errs, warns


#: 合成前必拦的问题码（#250 N-4 D）：这些组合**不可能出对的声**——进合成只会得到
#: 「别人的声 / 无声 / 引擎报错」，再被前端当成「疑似无声」；应在面板直接给 ⚠ 并指路
#: 语音页。skuio 机 Mizuki（``avatar_clone + ja-JP-NanamiNeural``、无录音）＝
#: ``clone_missing_reference``，合成日志 ``CER=0.719 应克隆未克隆`` 就是这么来的。
BLOCKING_PROBLEM_CODES = frozenset({
    "clone_missing_reference",   # 克隆态却没有任何录音 / speaker 身份
    "clone_backend_not_clone",   # 克隆态挂了非克隆引擎
    "preset_backend_is_clone",   # 预置态却挂克隆引擎
    "preset_voice_missing",      # 显式预置态没选音色
    "preset_voice_unknown",      # 音色名不在引擎合法表
    "invalid_mode",              # 三态以外的值
})

#: 只提示不拦：``clone_with_preset_voice``——克隆链（avatar_clone / hub 零样本）根本不读
#: ``voice``，参考音才驱动音色；钧机 Mizuki 正是「有录音 + 残留 zh-CN-XiaoxiaoNeural」，
#: 两天五次合成 Whisper 全文转出、CER≈0，声音是对的。保存校验仍按 L-2 A 判 error
#: （提示清理），但合成前拦它＝把一把好声音关掉。``clone_reference_file_missing`` 是
#: L-2 A 的 warning（托管 / hub 档参考音在远端），同样只提示。


def synth_gate(vp: Any, *, check_files: bool = False) -> Dict[str, Any]:
    """合成前配置闸（纯函数）：``validate_voice_profile`` 的问题清单分成
    ``blocking``（必拦）与 ``advisory``（只提示）。

    返回 ``{"blocked", "code", "detail", "blocking", "advisory"}``；``code`` 取第一条必拦
    问题码（给 ``reason=voice_config:<code>`` 与 ``[tts] verdict=block:config problem=<code>``）。
    ``check_files`` 缺省关：试听 / 发送链自己会报「参考音文件不在」的具体错，这里不重复。
    """
    problems = validate_voice_profile(vp, check_files=check_files)
    blocking = [p for p in problems
                if str(p.get("code") or "") in BLOCKING_PROBLEM_CODES
                and str(p.get("severity") or "error") != "warning"]
    advisory = [p for p in problems if p not in blocking]
    first = blocking[0] if blocking else {}
    return {
        "blocked": bool(blocking),
        "code": str(first.get("code") or ""),
        "detail": str(first.get("detail") or ""),
        "blocking": blocking,
        "advisory": advisory,
    }


_PRESET_KEEP_KEYS = ("instruct_style", "emotion", "format", "enabled")


def _preset_backend_for_voice(voice: str) -> str:
    v = _s(voice)
    if not v:
        return "edge_tts"
    try:
        from src.ai.edge_voice_catalog import OPENAI_VOICES
        if v.lower() in OPENAI_VOICES:
            return "openai"
    except Exception:
        pass
    return "edge_tts"


def _clone_has_identity(vp: Dict[str, Any]) -> bool:
    ref = _s(vp.get("reference_audio_path"))
    spk = _s(vp.get("speaker_id"))
    has_ref = bool(ref) and any(ch.isalnum() for ch in ref)
    has_spk = bool(spk) and any(ch.isalnum() for ch in spk)
    return has_ref or has_spk


def _preset_voice_from_illegal(vp: Dict[str, Any]) -> Dict[str, Any]:
    voice = _s(vp.get("voice"))
    out: Dict[str, Any] = {k: vp[k] for k in _PRESET_KEEP_KEYS if k in vp and vp[k] not in (None, "")}
    out["voice_mode"] = VOICE_MODE_PRESET
    out["backend"] = _preset_backend_for_voice(voice)
    out["voice"] = voice
    if "enabled" not in out:
        out["enabled"] = True
    return out


def coerce_voice_profile_for_import(
    vp: Any, *, check_files: bool = False,
) -> Tuple[Any, Dict[str, Any]]:
    """导入收口：非法 ``voice_profile`` → 可保存的合法档。不改入参。

    保存闸（PUT）仍拒绝非法组合（#205）。导入不能把「解析过了、写入 400」留给用户，
    也不能把还能用的克隆录音降成预置声。

    返回 ``(vp_out, info)``。``info`` 空＝未改写（含仅 warning）。改写时::

        {action, voice_problems, voice}

    ``action``：

    - ``preset``：克隆无录音但带了预置声名（Claire / skuio Mizuki）→ Edge/OpenAI 预置；
    - ``strip_preset_voice``：克隆身份齐备、只多了残留 Neural 名 → 清空 ``voice``、保留克隆；
    - ``off``：其余无法推断的非法档 → 不发语音。
    """
    problems = validate_voice_profile(vp, check_files=check_files)
    errs, _warns = split_problems(problems)
    if not errs:
        return vp, {}
    codes = [str(p.get("code") or "") for p in errs]
    src = dict(vp) if isinstance(vp, dict) else {}
    info_base = {"voice_problems": codes, "voice": _s(src.get("voice"))}

    if (
        "clone_missing_reference" not in codes
        and "clone_with_preset_voice" in codes
        and _clone_has_identity(src)
    ):
        out = dict(src)
        out["voice"] = ""
        out["voice_mode"] = VOICE_MODE_CLONE
        still, _ = split_problems(validate_voice_profile(out, check_files=False))
        if not still:
            return out, {**info_base, "action": "strip_preset_voice", "voice": ""}

    if _s(src.get("voice")) and _looks_like_preset_voice_name(_s(src.get("voice"))):
        candidate = _preset_voice_from_illegal(src)
        still, _ = split_problems(validate_voice_profile(candidate, check_files=False))
        if not still:
            return candidate, {**info_base, "action": "preset", "voice": candidate["voice"]}

    return dict(DEFAULT_NEW_VOICE_PROFILE), {**info_base, "action": "off", "voice": ""}


def apply_import_voice_coerce(
    persona: Dict[str, Any], *, check_files: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """对人设文档做导入语音收口。未改写时返回原对象；改写时浅拷贝并替换 ``voice_profile``。"""
    if not isinstance(persona, dict):
        return persona, {}
    new_vp, info = coerce_voice_profile_for_import(
        persona.get("voice_profile"), check_files=check_files)
    if not info:
        return persona, {}
    out = dict(persona)
    out["voice_profile"] = new_vp
    return out, info


def apply_new_persona_voice_default(persona: Dict[str, Any]) -> Dict[str, Any]:
    """新建人设：没带任何语音配置 → 写入 ``{voice_mode: off}``（D-L4）。

    带了 voice_profile（导入 / 复制 / 登记流）原样尊重；返回新 dict，不改入参。
    """
    if not isinstance(persona, dict):
        return persona
    vp = persona.get("voice_profile")
    if isinstance(vp, dict) and any(
            _s(vp.get(k)) for k in ("voice_mode", "backend", "voice", "speaker_id",
                                    "reference_audio_path")):
        return persona
    if isinstance(vp, dict) and vp.get("enabled") is not None:
        return persona
    out = dict(persona)
    out["voice_profile"] = dict(DEFAULT_NEW_VOICE_PROFILE)
    return out


def voice_mode_is_off(vp: Any) -> bool:
    """TTSPipeline / autosend 的单点判据：该人设是否「不发语音」（只认**显式**三态）。"""
    return isinstance(vp, dict) and _s(vp.get("voice_mode")).lower() == VOICE_MODE_OFF


def binding_summary(vp: Any) -> Dict[str, str]:
    """给 UI 的绑定文案素材：``{mode, basis, backend, voice, ref_name, lang, gender}``。"""
    mode, basis = derive_voice_mode(vp)
    vp = vp if isinstance(vp, dict) else {}
    ref = _s(vp.get("reference_audio_path"))
    out = {
        "mode": mode, "basis": basis,
        "backend": _s(vp.get("backend")).lower(),
        "voice": _s(vp.get("voice")),
        "ref_name": os.path.basename(ref) if ref else "",
        "lang": "", "gender": "",
    }
    if mode == VOICE_MODE_PRESET and out["voice"]:
        try:
            from src.ai.edge_voice_catalog import OPENAI_VOICES, voice_meta
            m = voice_meta(out["voice"])
            if m:
                out["lang"], out["gender"] = m["lang"], m["gender"]
            elif out["voice"].lower() in OPENAI_VOICES:
                out["gender"] = OPENAI_VOICES[out["voice"].lower()]
        except Exception:
            pass
    return out


__all__ = [
    "BLOCKING_PROBLEM_CODES",
    "DEFAULT_NEW_VOICE_PROFILE", "PRESET_BACKENDS", "VOICE_MODES",
    "VOICE_MODE_CLONE", "VOICE_MODE_OFF", "VOICE_MODE_PRESET",
    "apply_import_voice_coerce", "apply_new_persona_voice_default",
    "binding_summary", "coerce_voice_profile_for_import", "derive_voice_mode",
    "resolve_clone_backend", "split_problems", "synth_gate", "validate_voice_profile",
    "voice_mode_is_off",
]
