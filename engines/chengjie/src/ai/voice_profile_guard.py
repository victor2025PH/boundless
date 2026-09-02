"""克隆档「登记后挂载」守卫（#149，2026-09-02 钧机 KKXSTU/SY8TGE 诊断包）。

事故链（与 #93「启用标志丢失」/ #137「占位串」同族，但断点不同——在**登记之后**）：

1. 人设工作室抽屉打开某人设（语音页 backend 下拉=「继承全局」、``_loadedProfile``
   是登记前的快照）；
2. 用户在同一抽屉里用 ``<cp-voice mode="enroll">`` 完成克隆登记——服务端把
   ``voice_profile={enabled, owner_consent, backend: avatar_clone, speaker_id,
   reference_audio_path, source: avatar_zeroshot…}`` 写进人设，试听是正确的克隆声；
3. 用户接着点抽屉「保存」——表单只知道下拉里的 ``backend: ""`` / ``voice: ""``，
   deep-merge 后把 ``backend`` 顶成空串＝克隆声**被静默卸载**：
   ``persona_routes`` 启动统计「13 个人设 12 个配了克隆 backend」、工具箱下拉
   无 🎤、合成回落全局/标准声——「登记成功、试听正确、工具箱找不到」一根两果。

本模块是两道闸的纯函数核心（零 IO、零重依赖，persona_manager / persona_routes
都能直接 import）：

- 写闸 ``protect_registered_clone_patch``：表单补丁里的**空值**不得覆写已登记克隆档
  的身份键（backend/voice/speaker_id/reference_audio_path/enabled/owner_consent）。
  显式换成别的后端（edge_tts/openai…）、显式 ``enabled: False`` 是人的决定，放行；
  解绑走 ``DELETE /api/voice/profiles/{pid}``（整份摘除），不靠空串。
- 自愈 ``heal_clone_profile``：存量已被打坏的档（backend 空、但登记来源
  ``source=avatar_zeroshot`` / ``lan_zeroshot`` / ``command_args`` 在场）按来源推回
  backend；``enabled`` 键缺失且授权+参考音齐备补 True（与 #93 读取侧修补同口径，
  这里在写/载边界一次性修好，不必每次读都补）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# 克隆类后端全集（persona_voice.CLONE_BACKENDS 是本常量的再导出；voice_routes /
# lang_voice_route 的本地集合与此同口径）。
CLONE_BACKENDS = frozenset({
    "voice_clone_command", "coqui_http", "voice_clone_lan",
    "avatar_clone", "minicpm_clone",
})

# 登记来源 → 后端（voice_enroll.build_*_voice_profile 写入的 ``source`` 标记）
_SOURCE_TO_BACKEND = {
    "avatar_zeroshot": "avatar_clone",
    "lan_zeroshot": "voice_clone_lan",
}

# 表单补丁里「空值不得覆写登记结果」的身份键
_IDENTITY_KEYS = (
    "backend", "voice", "speaker_id", "reference_audio_path",
    "enabled", "owner_consent",
)


def _is_placeholder(v: Any) -> bool:
    s = str(v or "").strip()
    return bool(s) and not any(ch.isalnum() for ch in s)


def _has_voice_identity(vp: Dict[str, Any]) -> bool:
    """参考音或有效 speaker_id 至少一个在场（与 #93 修补/路由回查同判据）。"""
    ref = str(vp.get("reference_audio_path") or "").strip()
    spk = str(vp.get("speaker_id") or "").strip()
    if _is_placeholder(ref):
        ref = ""
    if _is_placeholder(spk):
        spk = ""
    return bool(ref or spk)


def infer_clone_backend(vp: Dict[str, Any]) -> str:
    """按登记痕迹推断克隆后端；推不出返回空串（绝不猜）。

    - ``source`` 标记（avatar_zeroshot → avatar_clone / lan_zeroshot → voice_clone_lan）
    - Qwen 命令式登记独有的 ``command_args`` / ``voice_profile_json_path``
      → voice_clone_command
    """
    if not isinstance(vp, dict):
        return ""
    src = str(vp.get("source") or "").strip().lower()
    if src in _SOURCE_TO_BACKEND:
        return _SOURCE_TO_BACKEND[src]
    if vp.get("command_args") or vp.get("voice_profile_json_path"):
        return "voice_clone_command"
    return ""


def is_registered_clone_profile(vp: Any) -> bool:
    """这份 voice_profile 是否是「登记成功过的克隆档」。

    判据：克隆类 backend（现存或可按登记痕迹推回）+ 参考音/speaker 在场 +
    ``enabled`` 未被显式置 False（显式 False＝运营手动停用，尊重）。
    """
    if not isinstance(vp, dict) or not vp:
        return False
    if vp.get("enabled") is False:
        return False
    backend = str(vp.get("backend") or "").strip().lower() or infer_clone_backend(vp)
    if backend not in CLONE_BACKENDS:
        return False
    return _has_voice_identity(vp)


def heal_clone_profile(vp: Any) -> Tuple[Any, List[str]]:
    """修复被打坏的已登记克隆档，返回 ``(新 dict, 修复键列表)``；无需修复原样返回入参。

    只修两种确定性损伤（都有登记痕迹作证，不是猜）：
    - ``backend`` 空/缺，但 ``source``/``command_args`` 指认了登记后端 → 补回；
    - ``enabled`` 键**不存在**（不是 False），且 owner_consent + 参考音/speaker 齐备
      → 补 True（#93 形态，读取侧修补同口径）。
    """
    if not isinstance(vp, dict) or not vp:
        return vp, []
    fixed: List[str] = []
    out = vp
    backend = str(vp.get("backend") or "").strip().lower()
    if not backend or _is_placeholder(backend):
        inferred = infer_clone_backend(vp)
        if inferred and _has_voice_identity(vp) and vp.get("enabled") is not False:
            out = dict(out)
            out["backend"] = inferred
            fixed.append("backend")
            backend = inferred
    if ("enabled" not in out and backend in CLONE_BACKENDS
            and bool(out.get("owner_consent")) and _has_voice_identity(out)):
        out = dict(out) if out is vp else out
        out["enabled"] = True
        fixed.append("enabled")
    return out, fixed


def protect_registered_clone_patch(
    existing_vp: Any, patch_vp: Any,
) -> Tuple[Any, List[str]]:
    """表单补丁 × 已登记克隆档：空值身份键从补丁里剥掉，返回 ``(新补丁, 被保留键)``。

    - existing 不是已登记克隆档 → 补丁原样返回（不改任何既有行为）；
    - 补丁里 ``backend/voice/speaker_id/reference_audio_path/enabled/owner_consent``
      为空串/None 且 existing 对应键非空 → 剥掉（保留登记结果）；
    - 非空值（换成 edge_tts、显式 ``enabled: False`` 等）＝人的决定，放行。
    """
    if not isinstance(patch_vp, dict) or not isinstance(existing_vp, dict):
        return patch_vp, []
    if not is_registered_clone_profile(existing_vp):
        return patch_vp, []
    preserved: List[str] = []
    out = dict(patch_vp)
    for k in _IDENTITY_KEYS:
        if k not in out:
            continue
        v = out[k]
        empty = v is None or (isinstance(v, str) and not v.strip())
        if not empty:
            continue
        ex = existing_vp.get(k)
        ex_empty = ex is None or (isinstance(ex, str) and not ex.strip())
        if ex_empty and k != "backend":
            continue   # 两边都空：剥不剥无差别，留给 deep-merge
        out.pop(k, None)
        preserved.append(k)
    return out, preserved


def protect_registered_clone_persona_patch(
    existing: Any, patch: Any,
) -> Tuple[Any, List[str]]:
    """人设级薄壳：只处理 ``patch["voice_profile"]``，其余字段不碰。"""
    if not isinstance(existing, dict) or not isinstance(patch, dict):
        return patch, []
    p_vp = patch.get("voice_profile")
    if not isinstance(p_vp, dict):
        return patch, []
    new_vp, preserved = protect_registered_clone_patch(
        existing.get("voice_profile"), p_vp)
    if not preserved:
        return patch, []
    out = dict(patch)
    if new_vp:
        out["voice_profile"] = new_vp
    else:
        out.pop("voice_profile", None)
    return out, preserved
