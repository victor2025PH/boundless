"""Resolve voice_output config for a given persona_id.

Three-tier fallback priority (highest → lowest):
  1. ``personas.<id>.voice_profile``   — per-persona voice clone / TTS settings
  2. ``telegram.voice_reply``           — TG-specific defaults (backend/voice/format)
  3. ``messenger_rpa.voice_output``     — legacy compat shim (kept ≥ 6 months)

Usage::

    from src.ai.persona_voice import resolve_voice_cfg
    cfg = resolve_voice_cfg(persona_id, config_manager.config)
    tts = TTSPipeline(cfg)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 克隆类后端集合（单一事实源在 voice_profile_guard；这里再导出供 voice_routes /
# lang_voice_route 既有 import 路径消费；#93 修补消费）
from src.ai.voice_profile_guard import CLONE_BACKENDS  # noqa: E402,F401

# UI 哨兵「系统通用音色」（2026-08-05 P0）：坐席下拉的第三种选择，介于
# 「空串=跟随会话人设回落链」与「显式人设 id」之间——**钉死全局默认配置**
# （resolve_voice_cfg(None)，即 telegram.voice_reply ⊎ messenger_rpa.voice_output），
# 不做会话/账号人设回落、不做 per-contact 会员档路由、不注入 persona_id。
# 由此获得两个硬保证：① 选它永远是同一把声音（与会话绑定无关）；② voice_cfg 无
# persona_id → tts_pipeline 的 hub 人设层（allowlist/voice_consistency=strict）
# 天然不参与 → 不会因 hub 单点故障被「宁缺毋滥」拒发——这正是「默认音色不可用」
# 事故的结构性出口。刻意**不**把空串改成这个语义：空串的回落链是既有会话的
# 听感事实源，重定义会让老会话静默换声（比不发语音更伤）。
SYSTEM_VOICE_ID = "__system__"


# Fields that pin a *specific* cloned/reference voice. When a persona switches
# to a different backend (e.g. a public neural voice via edge_tts), these must
# NOT leak from the inherited global clone profile — otherwise every persona
# would reuse the operator's own ``my_voice`` reference audio / consent flags.
_CLONE_BLEED_KEYS = (
    "reference_audio_path", "reference_text", "command_args", "command_template",
    "command_timeout_sec", "voice", "speaker_id", "voice_profile_json_path",
    "base_url", "model", "clone_path", "source", "language",
)


# voice_profile 里「指认一把具体声音」的字符串键——占位串清洗只看这些键
# （reference_text 等自由文本不参与，防误伤）。
_VOICE_ID_KEYS = ("backend", "voice", "speaker_id", "reference_audio_path")


def _strip_voice_placeholders(vp: Dict[str, Any]) -> Dict[str, Any]:
    """把 voice_profile 里的占位串键（如 ``voice: "___"``）剥掉，返回新 dict。

    #137/#140（2026-09-02）：人设工作室的半成品语音设置以占位串落库，
    人设层 merge 时覆盖全局克隆档 → 克隆声整机静默变 edge 通用声。
    读取侧统一口径：**占位串视同未设置**（判定 SSOT＝
    lang_voice_route.is_voice_placeholder）。无占位串时原样返回入参。
    """
    try:
        from src.ai.lang_voice_route import is_voice_placeholder
        dirty = [
            k for k in _VOICE_ID_KEYS
            if isinstance(vp.get(k), str) and is_voice_placeholder(vp[k])
        ]
        if not dirty:
            return vp
        out = dict(vp)
        for k in dirty:
            out.pop(k, None)
        logger.debug(
            "[persona_voice] voice_profile 占位串键视同未设置：%s", dirty)
        return out
    except Exception:
        return vp


def _merge_voice_profile(merged: Dict[str, Any], vp: Dict[str, Any]) -> bool:
    """Apply a persona ``voice_profile`` on top of an already merged voice cfg.

    If the persona explicitly selects a *different* backend than the inherited
    one, clone-specific fields are dropped first so a public neural voice does
    not accidentally reuse the global clone's reference audio / consent flags.

    Returns True when the profile actually contributed (voice_source 观测用)。
    """
    if not isinstance(vp, dict):
        return False
    # 占位串键（"___" 之类）视同未设置——剥掉后若一无所有，整份按空占位忽略，
    # 全局克隆档不被半成品人设配置覆盖（#137/#140）。
    vp = _strip_voice_placeholders(vp)
    # Ignore empty UI placeholders such as {backend:"", voice:""}.
    # voice_mode（L-2 #205 三态）算「真配置」：{voice_mode: off} 必须盖过全局层，
    # 否则新建人设的「不发语音」会被全局克隆档顶掉。
    if not any(vp.get(k) for k in (
        "enabled", "backend", "voice", "speaker_id", "reference_audio_path",
        "voice_mode",
    )):
        return False
    base_vp = dict(merged.get("voice_profile") or {})
    new_backend = str(vp.get("backend") or "").strip().lower()
    old_backend = str(base_vp.get("backend") or "").strip().lower()
    if new_backend and old_backend and new_backend != old_backend:
        for k in _CLONE_BLEED_KEYS:
            base_vp.pop(k, None)
        # Public/cloud neural backends carry no clone consent/enable semantics.
        if new_backend in ("edge_tts", "openai", "elevenlabs"):
            base_vp.pop("enabled", None)
            base_vp.pop("owner_consent", None)
    base_vp.update(vp)
    merged["voice_profile"] = base_vp
    # Persona may also override top-level TTS fields.
    for k in ("backend", "voice", "model", "format"):
        if vp.get(k):
            merged[k] = vp[k]
    return True


def resolve_voice_cfg(
    persona_id: Optional[str],
    full_config: Dict[str, Any],
    *,
    tier: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a ``voice_output``-style dict ready for ``TTSPipeline``.

    Merges layers bottom-up so higher-priority keys always win.
    Never raises; returns ``{}`` on any error so callers stay safe.

    P3：传入端用户会员 ``tier``（如 ``get_entitlement(contact_key)["tier"]``）后，
    按 ``voice_routing`` 策略改写后端（VIP→elevenlabs，免费→edge 降级省成本）。
    ``tier=None``（默认）或 ``voice_routing.enabled=false`` → 不路由，行为不变。
    """
    # UI 哨兵防御：任何路径把「系统通用音色」当人设 id 传进来都按 None 处理
    # （主消费口在 resolve_effective_voice_context；这里兜住散装调用方）。
    if str(persona_id or "").strip() == SYSTEM_VOICE_ID:
        persona_id = None
    try:
        # ── Layer 0 (lowest): messenger_rpa.voice_output compat shim ──
        mrpa_vo: Dict[str, Any] = dict(
            (full_config.get("messenger_rpa") or {}).get("voice_output") or {}
        )

        # ── Layer 1: telegram.voice_reply TG-specific overrides ──
        tg_vr: Dict[str, Any] = dict(
            (full_config.get("telegram") or {}).get("voice_reply") or {}
        )
        merged = {**mrpa_vo}
        for k, v in tg_vr.items():
            if v is not None:
                merged[k] = v

        # 生效音色来自哪一层（#137/#140 观测收口：合成 INFO 打「来源」用）。
        # 人设层真正贡献了配置才升格为 persona——半成品占位档被剥空时仍算全局。
        _voice_layer = "global"

        # ── Layer 2: config.yaml per-persona voice_profile ──
        quirks_str = ""
        # 人设性别（L-2 #205）：TTSPipeline 兜底选声必须同性别，随 cfg 注入；
        # 缺失＝性别未知（兜底沿用语种缺省声，旧行为）。
        gender_str = ""
        if persona_id:
            personas_cfg = full_config.get("personas") or {}
            profiles = personas_cfg.get("profiles") or []
            for p in profiles:
                if not isinstance(p, dict):
                    continue
                if p.get("id") != persona_id:
                    continue
                quirks_str = str(p.get("quirks") or "").strip()
                gender_str = str(p.get("gender") or "").strip()
                vp = p.get("voice_profile")
                if not isinstance(vp, dict):
                    break
                if _merge_voice_profile(merged, vp):
                    _voice_layer = f"persona:{persona_id}"
                break

        # ── Layer 3 (highest): runtime PersonaManager profiles ──
        # Web voice enrollment persists into profiles_runtime.yaml via PersonaManager,
        # not config.yaml. Read it here so "登记成功" immediately affects TTS sends.
        if persona_id:
            try:
                from src.utils.persona_manager import PersonaManager
                p_rt = PersonaManager.get_instance().get_persona_by_id(str(persona_id))
                if isinstance(p_rt, dict):
                    if _merge_voice_profile(merged, p_rt.get("voice_profile") or {}):
                        _voice_layer = f"persona:{persona_id}"
                    quirks_str = str(p_rt.get("quirks") or quirks_str).strip()
                    gender_str = str(p_rt.get("gender") or gender_str).strip()
            except Exception:
                pass

        if merged:   # 空配置仍返回 {}（既有契约：调用方以空 dict 判「无语音配置」）
            merged["voice_source_layer"] = _voice_layer
            if gender_str:
                merged["persona_gender"] = gender_str

        # 顶层音色占位串同样视同未设置（全局 voice_reply.voice 也可能被存成
        # "___"）——留着会被 TTSPipeline/路由当真实音色消费。
        try:
            from src.ai.lang_voice_route import is_voice_placeholder
            for _k in ("voice", "fallback_voice"):
                if is_voice_placeholder(str(merged.get(_k) or "")):
                    merged.pop(_k, None)
            _vp_top = merged.get("voice_profile")
            if isinstance(_vp_top, dict):
                _vp_clean = _strip_voice_placeholders(_vp_top)
                if _vp_clean is not _vp_top:
                    merged["voice_profile"] = _vp_clean
        except Exception:
            pass

        if quirks_str:
            merged["persona_quirks"] = quirks_str

        # ── 注入全局局域网克隆配置，让 TTSPipeline 实现「LAN 优先 → 云端兜底」──
        vcl = full_config.get("voice_clone_lan")
        if isinstance(vcl, dict) and vcl:
            merged["voice_clone_lan"] = dict(vcl)

        # ── 注入 MiniCPM-o 情感克隆主机配置（backend=minicpm_clone 时消费；异步语音消息）──
        mcc = full_config.get("minicpm_clone")
        if isinstance(mcc, dict) and mcc:
            merged["minicpm_clone"] = dict(mcc)

        # ── 注入 AvatarHub CosyVoice3 配置（本机 7852，backend=avatar_clone 时消费）──
        av = full_config.get("avatar_voice")
        if isinstance(av, dict) and av:
            merged["avatar_voice"] = dict(av)

        # ── 注入 RVC 变声配置（.176:6242；人设 voice_profile.rvc_voice 指定 66 音色之一时，
        #    在克隆输出 WAV 上再变声成该音色）。令牌走 svc_token_env（secrets），不入库。──
        rvc = full_config.get("rvc")
        if isinstance(rvc, dict) and rvc:
            merged["rvc"] = dict(rvc)

        # ── 注入人设 id：预渲染语音命中层（TTSPipeline._try_prerendered）的查找键 ──
        if persona_id and "persona_id" not in merged:
            merged["persona_id"] = str(persona_id)

        # ── 注入全局 ElevenLabs 配置（付费情感旗舰档，backend=elevenlabs 时消费）──
        el = full_config.get("elevenlabs")
        if isinstance(el, dict) and el:
            merged["elevenlabs"] = dict(el)

        # ── P3：注入 TTS 成本费率（供 provider_stats 记账，与是否路由无关）──
        routing_block = full_config.get("voice_routing")
        if isinstance(routing_block, dict):
            rates = routing_block.get("cost_per_1k_chars")
            if isinstance(rates, dict) and rates:
                merged["cost_per_1k_chars"] = dict(rates)

        # ── P3：按端用户档位分层路由（VIP→旗舰，免费→降级省成本）──
        if tier is not None:
            from src.ai.voice_routing import resolve_voice_routing, route_voice_backend
            routing = resolve_voice_routing(full_config)
            if routing.get("enabled"):
                merged = route_voice_backend(merged, tier=tier, routing=routing)

        return merged
    except Exception:
        return {}


def resolve_voice_cfg_for_contact(
    persona_id: Optional[str],
    full_config: Dict[str, Any],
    *,
    contact_key: Optional[str] = None,
) -> Dict[str, Any]:
    """便捷接缝：端用户 ``contact_key`` → 会员档 → 分层路由后的 ``voice_cfg``。

    各平台合成前**一处调用**，避免在 sender / voice_autosend / messenger 等处
    各自重复「``resolve_tier_for_contact`` → ``resolve_voice_cfg(tier=...)``」。

    ``contact_key=None`` / monetization 未就绪 / 异常 → ``tier=None`` → 不路由
    （零行为变更，与直接调 ``resolve_voice_cfg`` 等价）。
    """
    tier: Optional[str] = None
    if contact_key:
        try:
            from src.ai.voice_routing import resolve_tier_for_contact
            tier = resolve_tier_for_contact(contact_key)
        except Exception:
            tier = None
    return resolve_voice_cfg(persona_id, full_config, tier=tier)


def resolve_emotion_for_send(
    voice_cfg: Dict[str, Any],
    text: str,
    *,
    platform: str = "telegram",
    account_id: Optional[str] = None,
    chat_key: Optional[str] = None,
    intent: Optional[str] = None,
    csat: Optional[float] = None,
    persona: Optional[Dict[str, Any]] = None,
    peer_audio_emotion: Optional[Dict[str, Any]] = None,
) -> Any:
    """P4：合成前的共享情感接缝（sender / voice_autosend / unified_inbox 共用）。

    仅当 ``voice_cfg.emotion.enabled=true`` 才解析会话信号并派生 ``EmotionSpec``；
    否则返回 ``None`` → 调用点传 ``emotion=None`` → 走 neutral（**零行为变更**）。

    信号优先级（见 ``voice_emotion.derive_emotion``）：CSAT 极差 → intent → 文本线索
    → 关系阶段微调。``rel_stage`` 经 ``companion_context.resolve_funnel_stage`` 取，
    provider 未就绪 / 异常 → None（仍可用 text 线索派生，绝不抛给 TTS 主流程）。
    """
    try:
        emo_cfg = voice_cfg.get("emotion") if isinstance(voice_cfg, dict) else None
        if not isinstance(emo_cfg, dict) or not emo_cfg.get("enabled"):
            return None
        default = str(emo_cfg.get("default") or "warm").strip().lower()

        # 骂战态覆写（2026-08-22 实施54 P0-3，最高优先）：本轮 temper 命中
        # 辱骂 → 回怼文本绝不许用人设默认的 happy/playful 基调念（实录 03:44
        # 「你才傻逼呢」被渲染成 情绪happy——开心语调骂人比不发更穿帮）。
        # 单一事实源＝temper 骂战态登记表（skill_manager 注入 hint 时登记，
        # TTL 180s，键与 A/B 线 convo_key 同构）；无登记＝下方原判定链，
        # 字节级旧行为。feud（熔断冷处理收场）走 serious——居高临下的冷淡，
        # 不是火气。intensity 0.85 过强情绪阈值（hub 情感通道 / 7852 强情绪
        # 路径），确定性不掷签。
        try:
            from src.ai.voice_emotion import EmotionSpec
            from src.companion.temper import (
                fight_turn_kind,
                record_fight_voice_override,
            )
            # 键第三段只用本函数形参 chat_key——上游
            # resolve_effective_voice_context 传入时已并好 chat_key or
            # contact_key；此处若再写 contact_key（非本函数形参）会在
            # chat_key 为空时 NameError → 被外层 try 吞掉 → 覆写静默失效。
            _fight_key = (
                f"{str(platform or '')}:{str(account_id or '')}"
                f":{str(chat_key or '')}")
            _fk = fight_turn_kind(_fight_key)
            if _fk == "insult":
                record_fight_voice_override()
                return EmotionSpec("angry", intensity=0.85, pace="fast")
            if _fk == "feud":
                record_fight_voice_override()
                return EmotionSpec("serious", intensity=0.75)
            if _fk == "grudge":
                # 记仇期（2026-08-22 P1）：气没全消的端着——冷淡偏平，
                # 比熔断收场（0.75）轻一档；绝不许回暖档甜嗓念别扭话。
                record_fight_voice_override()
                return EmotionSpec("serious", intensity=0.65)
        except Exception:
            pass

        rel_stage: Optional[str] = None
        if chat_key:
            try:
                from src.utils.companion_context import resolve_funnel_stage
                rel_stage = resolve_funnel_stage(
                    account_id, chat_key, channel=platform or "telegram")
            except Exception:
                rel_stage = None

        from src.ai.voice_emotion import (
            derive_emotion,
            expression_policy,
            resolve_expressiveness,
        )
        # 表达力档位只决定「无线索时基线多收」（克制档→neutral）；线索情绪的
        # intensity 缩放在 TTSPipeline.synthesize 入口统一做一次（那里也接得到
        # 骂战覆写/调用方显式 emotion），避免两处叠缩。
        level = resolve_expressiveness(voice_cfg, persona=persona)
        policy = expression_policy(level)
        return derive_emotion(
            intent=intent, rel_stage=rel_stage, csat=csat,
            text=text, default=default, persona=persona,
            peer_audio_emotion=peer_audio_emotion,
            baseline_intensity=policy.baseline_intensity)
    except Exception:
        return None


def resolve_effective_voice_context(
    full_config: Dict[str, Any],
    *,
    persona_id: Optional[str] = None,
    chat_key: Optional[str] = None,
    account_persona_id: Optional[str] = None,
    contact_key: Optional[str] = None,
    platform: str = "telegram",
    account_id: Optional[str] = None,
    text: str = "",
    intent: Optional[str] = None,
    csat: Optional[float] = None,
    peer_audio_emotion: Optional[Dict[str, Any]] = None,
    expressiveness: Any = None,
) -> Dict[str, Any]:
    """Resolve the actual persona, voice config, and emotion used for one send.

    This is the shared decision point for manual inbox voice, Telegram auto voice,
    and System Z autosend voice. ``persona_id`` is an explicit UI/operator choice;
    otherwise we fall back to chat binding, then account persona, then defaults.

    ``peer_audio_emotion``：上一条客户语音的声学情绪（见 speech_emotion），让出站情感声
    回应「听到的语气」。未提供 → 行为不变。

    ``expressiveness``：会话级表达力覆写（收件箱「本会话收着点」）；未提供时走会话
    覆写表 → 人设 voice_profile.expressiveness → 全局 emotion.expressiveness → natural。
    解出的档位写回 ``voice_cfg["voice_expressiveness"]``，TTSPipeline 与情绪派生读同一份。
    """
    cfg = full_config or {}
    resolved_persona: Dict[str, Any] = {}
    resolved_id = str(persona_id or "").strip()
    source = "explicit" if resolved_id else "fallback"
    # 「系统通用音色」哨兵：钉死全局默认，绕过人设回落链与 per-contact 路由
    # （语义与保证见模块顶 SYSTEM_VOICE_ID 注释）。persona_id 置空 → hub 人设层
    # / 预渲染命中层天然跳过；variety_key / 情绪解析仍走共享尾部（听感连续性
    # 与情绪基线是会话属性，不随「选哪把声音」变）。
    pin_system = resolved_id == SYSTEM_VOICE_ID
    if pin_system:
        resolved_id = ""
        source = "system"
    if not pin_system:   # 钉死全局时人设层完全不参与
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            if resolved_id:
                p = pm.get_persona_by_id(resolved_id)
                if isinstance(p, dict):
                    resolved_persona = p
            else:
                # 会话级覆写（与文本出站链同一优先级：conv_override > account >
                # legacy chat > domain）——开关关/键缺失时 _conv_key 为空，行为不变。
                _conv_key = ""
                if chat_key and account_id and conv_override_enabled(cfg):
                    _conv_key = conv_binding_key(
                        str(platform or ""), str(account_id or ""), str(chat_key))
                p, tier = pm.get_persona_with_tier(
                    str(chat_key or ""), str(account_persona_id or ""),
                    conversation_key=_conv_key)
                if isinstance(p, dict):
                    resolved_persona = p
                    resolved_id = str(p.get("id") or "").strip()
                    source = str(tier or source)
        except Exception:
            resolved_persona = {}

    if pin_system:
        # 刻意用裸 resolve_voice_cfg(None)（不走 for_contact 的会员档路由）：
        # 「系统通用音色」的价值就是 100% 可预测——同一台机器上任何会话选它
        # 都得到同一份配置；VIP 分层属人设/回落链路径的优化，不属于这里。
        voice_cfg = resolve_voice_cfg(None, cfg)
    else:
        voice_cfg = resolve_voice_cfg_for_contact(
            resolved_id or None, cfg, contact_key=contact_key)
        # Inline/snapshot bindings can carry a voice_profile without an id. Merge
        # it directly so legacy chat bindings still get their own voice.
        if isinstance(resolved_persona, dict):
            if _merge_voice_profile(
                    voice_cfg, resolved_persona.get("voice_profile") or {}):
                voice_cfg["voice_source_layer"] = (
                    f"persona:{resolved_id}" if resolved_id else "persona:inline")
            # 人设性别随行（L-2 #205 兜底同性别选声）；resolve_voice_cfg 已注入时不覆盖
            _g = str(resolved_persona.get("gender") or "").strip()
            if _g and voice_cfg and not voice_cfg.get("persona_gender"):
                voice_cfg["persona_gender"] = _g
        # #93（2026-09-01 女王会话实锤）：克隆档「齐备但 enabled 键缺失」修补。
        # TTSPipeline._effective_backend 只认 enabled=true——克隆四件套（克隆
        # backend + 参考音/speaker + 授权）都在、唯独 enabled 键在某次 merge/
        # 迁移/手编中丢失时，克隆**根本不进场**、顶层静默回落标准声，且因为
        # 从未尝试过克隆连 fallback_from 都不记（前端一片绿、客户听陌生声）。
        # enabled 显式 False＝运营手动停用，绝不碰；只补「键不存在」的档。
        try:
            _vp_fix = voice_cfg.get("voice_profile")
            if (isinstance(_vp_fix, dict) and "enabled" not in _vp_fix
                    and str(_vp_fix.get("backend") or "").strip().lower()
                    in CLONE_BACKENDS
                    and bool(_vp_fix.get("owner_consent"))
                    and (str(_vp_fix.get("reference_audio_path") or "").strip()
                         or str(_vp_fix.get("speaker_id") or "").strip())):
                _vp_fix["enabled"] = True
                logger.warning(
                    "[persona_voice] #93 克隆档 enabled 键缺失已就地补齐"
                    "（persona=%s backend=%s）——此前该档静默回落标准声",
                    resolved_id or "-", _vp_fix.get("backend"))
        except Exception:
            logger.debug("[persona_voice] 克隆档 enabled 修补跳过", exc_info=True)

    # 生效音色来源标签（#137/#140 强制观测）：TTSPipeline 每次合成的 INFO 行
    # 据此打「人设X/全局/会话覆盖」——占位串事故里「配置到底谁在生效」全靠
    # 猜，这行让下一次诊断包直接给出答案。层（voice_source_layer，谁贡献了
    # 音色配置）× 人设解析档（persona_source，为什么选中这个人设）合成一个
    # 人话标签。
    _layer = str(voice_cfg.get("voice_source_layer") or "global")
    if pin_system:
        voice_cfg["voice_source"] = "全局(系统通用音色)"
    elif _layer.startswith("persona:"):
        _pname = _layer.split(":", 1)[1] or resolved_id or "?"
        voice_cfg["voice_source"] = (
            f"会话覆盖(人设{_pname})" if source == "conv_override"
            else f"人设{_pname}")
    else:
        voice_cfg["voice_source"] = "全局"

    # 会话口味键（voice_opener_guard，P0-2 2026-08-03）：三条语音链（A 线
    # voice_reply / B 线 autosend / 坐席手动）都经本解析器 → 在此注入一次，
    # 调用方零改动同享「同一会话跨消息开场词去重」。键与人设无关——标识的是
    # 同一个客户的听感连续性；无 chat/contact 上下文（预渲染/试听）保持为空。
    _vk = str(chat_key or contact_key or "").strip()
    if _vk and not str(voice_cfg.get("variety_key") or "").strip():
        voice_cfg["variety_key"] = (
            f"{str(platform or '')}:{str(account_id or '')}:{_vk}")

    # The pinned emotion baseline lives on the *resolved* voice_profile (which an
    # inline binding inherits from its profile by id). Surface it to the emotion
    # layer so the auto-reply path honors the same baseline as explicit selection.
    emo_persona = dict(resolved_persona) if isinstance(resolved_persona, dict) else {}
    vp_eff = voice_cfg.get("voice_profile")
    if isinstance(vp_eff, dict) and vp_eff.get("emotion") and not (
        (emo_persona.get("voice_profile") or {}).get("emotion")
    ):
        _evp = dict(emo_persona.get("voice_profile") or {})
        _evp["emotion"] = vp_eff["emotion"]
        emo_persona["voice_profile"] = _evp

    # 表达力档位（会话 > 人设 > 全局）在此解一次并写回 voice_cfg：情绪派生、
    # TTSPipeline 各表达层、气泡可解释信息都读同一份。
    try:
        from src.ai.voice_emotion import resolve_expressiveness
        _sess_level = expressiveness
        if _sess_level is None and chat_key and account_id:
            _sess_level = get_session_expressiveness(
                str(platform or ""), str(account_id or ""), str(chat_key))
        voice_cfg["voice_expressiveness"] = resolve_expressiveness(
            voice_cfg, persona=emo_persona or None, session=_sess_level)
        voice_cfg["voice_expressiveness_source"] = (
            "session" if _sess_level else (
                "persona" if (vp_eff or {}).get("expressiveness")
                or (emo_persona.get("voice_profile") or {}).get("expressiveness")
                else "global"))
    except Exception:
        logger.debug("[persona_voice] 表达力档位解析跳过", exc_info=True)

    emotion = resolve_emotion_for_send(
        voice_cfg, text, platform=platform, account_id=account_id,
        chat_key=chat_key or contact_key, intent=intent, csat=csat,
        persona=emo_persona or None, peer_audio_emotion=peer_audio_emotion,
    )
    return {
        "persona_id": resolved_id,
        "persona": resolved_persona,
        "persona_source": source,
        "voice_cfg": voice_cfg,
        "emotion": emotion,
        "expressiveness": voice_cfg.get("voice_expressiveness"),
    }


def check_voice_selection(
    requested_persona_id: Optional[str],
    voice_ctx: Dict[str, Any],
) -> str:
    """选声金标（#149，2026-09-02）：用户显式选了人设 X，合成路由必须落在 X 上。

    返回空串＝一致；否则返回机器可读原因（调用方据此**拒绝合成**而不是静默换声）：
      - ``persona_not_found``  显式 id 在人设库里不存在（被删/改名/陈旧偏好）——
                               旧行为是静默回落全局音色、UI 仍显示所选名字；
      - ``persona_mismatch``   解析出的 persona_id ≠ 请求 id（任何路由层偷换）；
      - ``source_mismatch``    生效音色来源标签指认了**另一个**人设（合成 INFO 行
                               「来源=人设Y」与用户选择 X 对不上＝P0 级信任破坏）。

    未显式选择（空/None）或选「系统通用音色」哨兵 → 不检查（回落链/钉死全局是
    既定语义）。纯函数、绝不抛。
    """
    req = str(requested_persona_id or "").strip()
    if not req or req == SYSTEM_VOICE_ID:
        return ""
    try:
        ctx = voice_ctx or {}
        resolved = str(ctx.get("persona_id") or "").strip()
        if resolved != req:
            return "persona_mismatch"
        if not isinstance(ctx.get("persona"), dict) or not ctx.get("persona"):
            return "persona_not_found"
        vc = ctx.get("voice_cfg") or {}
        layer = str(vc.get("voice_source_layer") or "")
        if layer.startswith("persona:"):
            owner = layer.split(":", 1)[1].strip()
            if owner and owner != req:
                return "source_mismatch"
        src = str(vc.get("voice_source") or "")
        if src.startswith("人设") and src[2:].strip() not in ("", req):
            return "source_mismatch"
        if src.startswith("会话覆盖(人设") and not src.startswith(f"会话覆盖(人设{req})"):
            return "source_mismatch"
        return ""
    except Exception:
        return ""


def default_account_persona_id(
    full_config: Optional[Dict[str, Any]],
    platform: str = "",
) -> str:
    """上线/无人设账号的默认人设 id（只读配置，不写库）。

    优先级：
      1. ``platform_login.default_persona_id``（跨平台运营默认）
      2. ``accounts.default_persona_id``（别名）
      3. ``config[platform].persona_ids[0]``（平台静态默认）
    """
    cfg = full_config or {}
    try:
        pl = cfg.get("platform_login") or {}
        pid = str(pl.get("default_persona_id") or "").strip()
        if pid:
            return pid
    except Exception:
        pass
    try:
        acct = cfg.get("accounts") or {}
        pid = str(acct.get("default_persona_id") or "").strip()
        if pid:
            return pid
    except Exception:
        pass
    try:
        _dpids = (cfg.get(str(platform or "").lower(), {}) or {}).get(
            "persona_ids") or []
        if _dpids:
            return str((_dpids[0] if _dpids else "") or "").strip()
    except Exception:
        pass
    return ""


def ensure_account_default_persona(
    registry: Any,
    platform: str,
    account_id: str,
    full_config: Optional[Dict[str, Any]],
) -> str:
    """账号上线前：meta 无人设则写入默认人设并返回生效 id。

    已有 ``persona_id`` / ``persona_ids`` 则不动（尊重运营显式绑定）。
    写库失败 / 无默认配置 → 返回空串，绝不抛。
    """
    plat = str(platform or "").lower().strip()
    aid = str(account_id or "").strip()
    if not plat or not aid or registry is None:
        return ""
    try:
        row = registry.get(plat, aid) or {}
        meta = row.get("meta") or {}
        # 实施72（2026-08-27 身份错乱事故）：身份待确认（identity_pending）的账号
        # **绝不**自动补挂人设——登录位换人后 58 秒内新身份就披上默认人设，是
        # 「AI 以错误身份说话」的放大器。转正（confirm_account_identity）时由人
        # 显式选择人设，或届时才补默认。已有显式绑定仍如实返回（下方 existing 分支）。
        if bool(meta.get("identity_pending")) and not str(
                meta.get("persona_id") or "").strip():
            try:
                import logging
                logging.getLogger(__name__).info(
                    "[persona] 账号身份待确认，跳过自动补挂默认人设 "
                    "platform=%s account=%s", plat, aid)
            except Exception:
                pass
            return ""
        existing = str(meta.get("persona_id") or "").strip()
        if not existing:
            for p in (meta.get("persona_ids") or []):
                existing = str(p or "").strip()
                if existing:
                    break
        if existing:
            return existing
        # ── #156（2026-09-03）：新账号不自动绑定人设 ──────────────────────
        # 「上线补默认人设」是便利功能，代价是**用户从没选过，AI 就已经以某个
        # 身份在说话了**：账号栏看不出这号用的是谁（显示的是自动补的那个），
        # 换人设要先意识到「原来已经绑了」。与 #63「按账号确认接管」同一哲学
        # ——身份和接管方式都该是人的显式决定。未选期间返回空串：账号栏显示
        # 「未选人设」引导选择，AI 不以任何身份代答（无人设 → 上游各链自然
        # 降级；A 线 auto_ai 亦不会披着别人的皮上阵）。
        # 显式开 ``platform_login.auto_attach_default_persona: true`` 回旧行为
        # （批量铺号的部署仍可要便利，但那是显式选择）。
        if not _auto_attach_enabled(full_config):
            try:
                import logging
                logging.getLogger(__name__).info(
                    "[persona] 新账号等待用户选择人设（#156 不自动绑定）"
                    " platform=%s account=%s", plat, aid)
            except Exception:
                pass
            return ""
        default = default_account_persona_id(full_config, plat)
        if not default:
            return ""
        registry.upsert(
            plat, aid,
            meta={"persona_id": default, "persona_ids": [default]},
            merge_meta=True,
        )
        try:
            import logging
            logging.getLogger(__name__).info(
                "[persona] 上线补默认人设 platform=%s account=%s persona=%s",
                plat, aid, default,
            )
        except Exception:
            pass
        return default
    except Exception:
        return ""


def _auto_attach_enabled(full_config: Optional[Dict[str, Any]]) -> bool:
    """``platform_login.auto_attach_default_persona``（#156 起默认 False）。

    True＝回到「上线自动补默认人设」的旧行为（批量铺号部署可显式要这份便利）。
    读不到按新行为（不自动绑）——身份是人的显式决定，判不出时宁可等人选。
    """
    try:
        pl = (full_config or {}).get("platform_login") or {}
        return bool(pl.get("auto_attach_default_persona", False))
    except Exception:
        return False


def account_persona_unselected(
    full_config: Optional[Dict[str, Any]],
    platform: str,
    account_id: str,
    *,
    registry: Any = None,
) -> bool:
    """该账号是否**尚未由人选定人设**（#156，2026-09-03）。

    判据＝注册表 meta 里没有显式绑定（``persona_id`` / ``persona_ids``）。
    刻意**不看**配置里的全局默认——那正是问题所在：配置默认让「没人选过」
    看起来像「已经选好了」，AI 于是披着一个用户从未挑过的身份上阵。

    两个消费方：账号栏显示「未选人设」引导选择；
    ``effective_automation`` 据此封顶 review（未选期间 AI 不代答，与 #63
    「按账号确认接管」同哲学——身份和接管方式都得是人的显式决定）。

    ``auto_attach_default_persona`` 开＝运营要旧的自动绑定便利 → 恒 False
    （那种部署里「没绑」只是还没上线过，不该拦）。

    **注册表里根本没有这一行 → False**（不是「没选」而是「不知道」）：
    A 线 telegram default 号、测试/CLI 装配、注册表暂不可用都属这类，把它们
    一律封成 review 就是拿判不出当判有罪。判不出一律 fail-open——绝不因为
    判定本身出错把在跑的账号静默降级。
    """
    try:
        if _auto_attach_enabled(full_config):
            return False
        plat = str(platform or "").strip().lower()
        aid = str(account_id or "").strip()
        if not plat or not aid:
            return False
        if registry is None:
            from src.integrations.account_registry import get_account_registry
            registry = get_account_registry()
        row = registry.get(plat, aid)
        if not row:
            return False          # 无此行＝判不出（见 docstring），不是未选
        meta = row.get("meta") or {}
        if str(meta.get("persona_id") or "").strip():
            return False
        for p in (meta.get("persona_ids") or []):
            if str(p or "").strip():
                return False
        return True
    except Exception:
        return False


def conv_override_enabled(full_config: Optional[Dict[str, Any]]) -> bool:
    """会话级人设覆写总开关（``inbox.persona_conv_override.enabled``，默认关）。

    关 = 出站链完全等同旧行为（只看账号人设/legacy chat 绑定）——既是灰度闸，
    也是事故 kill-switch（关掉后所有已写入的会话覆写立即失效但不删除）。
    """
    try:
        cfg = full_config or {}
        return bool(
            ((cfg.get("inbox") or {}).get("persona_conv_override") or {})
            .get("enabled", False)
        )
    except Exception:
        return False


def conv_binding_key(platform: str, account_id: str, chat_key: str) -> str:
    """会话级人设绑定键：``platform:account_id:chat_key``（3 段）。

    与 ``src.inbox.normalizer.conv_id`` 刻意同构（本模块不 import inbox 层防环；
    一致性由 tests/test_persona_effective.py 门禁钉住）。3 段键与 legacy
    peer-global 绑定键（裸 chat_id / ``line_rpa:xxx`` 2 段）天然无碰撞，
    可安全共存于 PersonaManager._chat_bindings 同一存储（bindings_runtime.yaml）。
    """
    return f"{platform}:{account_id}:{chat_key}"


# conv_binding_key 首段的合法平台集（会话覆写只会由统一收件箱会话产生，
# 平台集与 inbox normalizer 同源）。legacy 键即便自带冒号（line_rpa:U123 /
# web:visitor42）首段也不在此集合或段数不足 3，不会被误判。
_CONV_KEY_PLATFORMS = frozenset(
    {"telegram", "whatsapp", "line", "messenger", "web"}
)


def is_conv_binding_key(binding_key: str) -> bool:
    """判别绑定键是否为 3 段会话覆写键（legacy 清理工具的盘点判据）。

    规则：``split(":", 2)`` 出满 3 个非空段 **且** 首段 ∈ 已知平台集。
    宁可漏判（未知平台的 3 段键按 legacy 对待，只是多列一行）不可错判
    （把 legacy 键当覆写键会让清理工具漏掉真正的债）。
    """
    parts = str(binding_key or "").split(":", 2)
    return (
        len(parts) == 3
        and all(p.strip() for p in parts)
        and parts[0].strip().lower() in _CONV_KEY_PLATFORMS
    )


# ── 会话级表达力覆写（收件箱「本会话收着点 / 放开点」）────────────────────
# 与 conv_binding_key 同键（platform:account_id:chat_key）；进程内 dict +
# ``voice_expressiveness_runtime.json``（与 config.yaml 同目录）落盘，重启不丢。
# 只存档位字串，不存 EmotionSpec——档位是运营语言，spec 是引擎细节。
SESSION_EXPRESSIVENESS_FILENAME = "voice_expressiveness_runtime.json"
_session_expr: Dict[str, str] = {}
_session_expr_loaded = False


def _session_expr_path() -> Path:
    env_path = os.environ.get("AITR_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser().parent / SESSION_EXPRESSIVENESS_FILENAME
    env_dir = os.environ.get("AITR_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "config" / SESSION_EXPRESSIVENESS_FILENAME
    return Path(__file__).resolve().parents[2] / "config" / SESSION_EXPRESSIVENESS_FILENAME


def _session_expr_load() -> None:
    global _session_expr_loaded
    if _session_expr_loaded:
        return
    _session_expr_loaded = True
    try:
        p = _session_expr_path()
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                _session_expr.update(
                    {str(k): str(v) for k, v in data.items() if v})
    except Exception:
        logger.debug("[persona_voice] 会话表达力覆写表读取失败", exc_info=True)


def _session_expr_save() -> None:
    try:
        p = _session_expr_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(_session_expr, ensure_ascii=False, indent=1),
            encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.debug("[persona_voice] 会话表达力覆写表落盘失败", exc_info=True)


def get_session_expressiveness(
    platform: str, account_id: str, chat_key: str,
) -> str:
    """该会话的表达力覆写档位；无覆写返回 ``""``。永不抛。"""
    try:
        _session_expr_load()
        return _session_expr.get(conv_binding_key(platform, account_id, chat_key), "")
    except Exception:
        return ""


def set_session_expressiveness(
    platform: str, account_id: str, chat_key: str, level: Any,
) -> str:
    """写/清会话覆写。``level`` 空/None/"inherit" = 清除（回落人设/全局）。

    返回规范化后的档位（清除时 ``""``）。
    """
    from src.ai.voice_emotion import normalize_expressiveness
    _session_expr_load()
    key = conv_binding_key(platform, account_id, chat_key)
    raw = str(level or "").strip().lower()
    if not raw or raw in ("inherit", "auto", "default_inherit", "none", "null"):
        _session_expr.pop(key, None)
        _session_expr_save()
        return ""
    lv = normalize_expressiveness(raw)
    if not lv:
        _session_expr.pop(key, None)
    else:
        _session_expr[key] = lv
    _session_expr_save()
    return lv


def resolve_effective_persona(
    full_config: Dict[str, Any],
    platform: str,
    account_id: str,
    chat_key: str = "",
    *,
    registry: Any = None,
) -> Tuple[str, str]:
    """出站链单一事实源：这条会话到底以谁的身份说话。

    返回 ``(persona_id, tier)``：
      - ``("chen_mo", "conv_override")``   — 会话级覆写命中（开关开 + 绑定存在 + profile 有效）
      - ``("lin_xiaoyu", "account_profile")`` — 账号级人设（registry meta / config）
      - ``("", "")``                        — 都没有（调用方回落 legacy chat 绑定 / 域默认，
                                              即 PersonaManager 内部既有链）

    覆写命中但 profile 已被删除 → 视同未覆写（回落账号级），绝不让出站链
    拿到悬空 id。任何异常按下一档降级，永不抛。
    """
    cfg = full_config or {}
    ck = str(chat_key or "").strip()
    pid, tier = "", ""
    if ck and conv_override_enabled(cfg):
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            key = conv_binding_key(
                str(platform or "").strip(), str(account_id or "").strip(), ck
            )
            ref = pm.get_chat_binding_ref(key)
            if ref and pm.get_persona_by_id(ref) is not None:
                pid, tier = ref, "conv_override"
        except Exception:
            pass
    if not pid:
        pid = resolve_account_persona_id(
            cfg, platform, account_id, registry=registry
        )
        tier = "account_profile" if pid else ""
    _record_resolve_observation(platform, ck, tier)
    return pid, tier


def _record_resolve_observation(platform: str, chat_key: str, tier: str) -> None:
    """resolve 观测打点（best-effort，绝不影响解析结果）。

    legacy_present：这条会话存在 legacy peer-global 绑定（引用式或内联快照）——
    与 tier 一起交给 stats 判「被压制」口径（tier 非空才算压制；tier 空时
    legacy 会在调用方回落链里真的生效）。
    """
    try:
        from src.ai.persona_override_stats import get_persona_override_stats
        legacy_present = False
        if chat_key:
            try:
                from src.utils.persona_manager import PersonaManager
                legacy_present = PersonaManager.get_instance().has_chat_binding(
                    chat_key
                )
            except Exception:
                legacy_present = False
        get_persona_override_stats().record_resolve(
            tier, platform, legacy_present
        )
    except Exception:
        pass


def resolve_effective_persona_id(
    full_config: Dict[str, Any],
    platform: str,
    account_id: str,
    chat_key: str = "",
    *,
    registry: Any = None,
) -> str:
    """``resolve_effective_persona`` 的 id-only 薄壳（出站链调用点用）。"""
    return resolve_effective_persona(
        full_config, platform, account_id, chat_key, registry=registry
    )[0]


def resolve_account_persona_id(
    full_config: Dict[str, Any],
    platform: str,
    account_id: str,
    *,
    registry: Any = None,
) -> str:
    """Resolve the effective account-level persona id for a platform account.

    Single source of truth for "which persona does this account speak as",
    shared by autosend-voice gating and auto-draft enrichment so both agree.

    Priority (highest → lowest):
      1. registry ``meta.persona_id``   — explicit singular binding
      2. registry ``meta.persona_ids[0]`` — plural list written by
         ``TelegramAccountRegistry.sync_to_account_registry`` (config sync)
      3. ``platform_login.default_persona_id`` / ``accounts.default_persona_id``
      4. ``config[platform].persona_ids[0]`` — static config default

    Fixes the plural/singular mismatch root cause: sync writes ``persona_ids``
    (list) but callers historically read ``persona_id`` (scalar) → empty
    ``_real_pid`` → voice grey-list allowlist mis-blocks → "发语音却只收到文字".
    Best-effort: any lookup failure degrades to the next tier, never raises.
    """
    cfg = full_config or {}
    try:
        if registry is None:
            from src.integrations.account_registry import get_account_registry
            registry = get_account_registry()
        row = registry.get(platform, account_id) or {}
        meta = row.get("meta") or {}
        pid = str(meta.get("persona_id") or "").strip()
        if pid:
            return pid
        _pids = meta.get("persona_ids") or []
        if _pids:
            pid = str((_pids[0] if _pids else "") or "").strip()
            if pid:
                return pid
    except Exception:
        pass
    return default_account_persona_id(cfg, platform)


def get_voice_profile_for_persona(
    persona_id: Optional[str],
    full_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Return only the ``voice_profile`` sub-dict for clone settings."""
    cfg = resolve_voice_cfg(persona_id, full_config)
    vp = cfg.get("voice_profile")
    return dict(vp) if isinstance(vp, dict) else {}


def persona_display_name(persona_id: Optional[str]) -> str:
    """人设显示名（best-effort；查不到/异常一律回空串，绝不抛）。

    P1-3（2026-08-02）：出站语音镜像行把「谁的音色在说话」带进 ``sender_name``
    （A 线 voice_reply / B 线 autosend / 坐席手动语音三条链共用本函数）——
    坐席在气泡上能看到语音出自哪个人设，音色错绑一眼可见。
    """
    pid = str(persona_id or "").strip()
    if not pid:
        return ""
    try:
        from src.utils.persona_manager import PersonaManager
        p = PersonaManager.get_instance().get_persona_by_id(pid)
        return str(p.get("name") or "").strip() if isinstance(p, dict) else ""
    except Exception:
        return ""
