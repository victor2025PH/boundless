"""语音情感层 — 把「会话上下文」翻成「TTS 引擎可消费的情绪控制」。

为什么需要：edge_tts / qwen / fish_speech 都是**平读**，听起来像机器人。
真正决定「像不像真人」的是**情绪表达**——同一句话，问候要温暖、投诉安抚要共情、
报喜要雀跃。本模块把 intent / 关系阶段 / CSAT / 文本线索 派生成一个统一的
``EmotionSpec``，再按各家引擎的能力翻成对应控制信号：

  - OpenAI gpt-4o-mini-tts → ``instructions`` 自然语言指令（"用温暖、略带笑意的语气说"）
  - ElevenLabs v3          → 内联音频标签（``[warmly]`` / ``[laughs]`` / ``[sighs]``）
  - edge_tts               → SSML 风格的 rate/pitch 调节（近似情绪）
  - 其余引擎               → 暂无情绪通道，返回原文不破坏

设计原则：
  - **纯函数、无 IO/网络**，可单测（与 voice_clone_client 的 build_* 同风格）。
  - **防御式**：任何脏输入都退化成 ``neutral``，绝不抛异常给 TTS 主流程。
  - **向后兼容**：``neutral`` 的映射 == 不加任何控制（行为与升级前一致）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

# 受支持的情绪词表（保持精简——只收各引擎都能稳定表达的）。
# 每个情绪带一组「画像」：自然语言语气词、ElevenLabs 标签、edge rate/pitch 偏移。
EMOTIONS = (
    "neutral", "warm", "happy", "excited", "playful",
    "empathetic", "apologetic", "calm", "sad", "serious",
)

# 情绪 → 各引擎画像。rate/pitch 为 edge_tts 的相对偏移（百分比/Hz 风格字符串生成用）。
_EMOTION_PROFILE: Dict[str, Dict[str, Any]] = {
    "neutral":    {"tone": "",                          "el_tag": "",            "rate": 0,   "pitch": 0},
    "warm":       {"tone": "温暖、亲切、略带笑意",        "el_tag": "warmly",      "rate": -4,  "pitch": 2},
    "happy":      {"tone": "愉快、轻松、带着微笑",        "el_tag": "happily",     "rate": 4,   "pitch": 4},
    "excited":    {"tone": "兴奋、雀跃、充满活力",        "el_tag": "excited",     "rate": 8,   "pitch": 6},
    "playful":    {"tone": "俏皮、活泼、带点调侃",        "el_tag": "playfully",   "rate": 5,   "pitch": 5},
    "empathetic": {"tone": "共情、柔和、关切",            "el_tag": "sympathetic", "rate": -8,  "pitch": -2},
    "apologetic": {"tone": "诚恳、歉意、放低姿态",        "el_tag": "apologetic",  "rate": -6,  "pitch": -2},
    "calm":       {"tone": "平静、沉稳、舒缓",            "el_tag": "calmly",      "rate": -6,  "pitch": -1},
    "sad":        {"tone": "低落、轻声、略带叹息",        "el_tag": "sadly",       "rate": -10, "pitch": -4},
    "serious":    {"tone": "认真、郑重、清晰",            "el_tag": "seriously",   "rate": -2,  "pitch": -1},
}


@dataclass(frozen=True)
class EmotionSpec:
    """一次合成的情绪规格。``intensity`` ∈ [0,1] 缩放强度。"""
    emotion: str = "neutral"
    intensity: float = 0.6
    pace: str = "normal"  # slow | normal | fast

    def __post_init__(self) -> None:
        # frozen dataclass：用 object.__setattr__ 做规整
        emo = str(self.emotion or "neutral").strip().lower()
        if emo not in EMOTIONS:
            emo = "neutral"
        object.__setattr__(self, "emotion", emo)
        try:
            inten = float(self.intensity)
        except (TypeError, ValueError):
            inten = 0.6
        object.__setattr__(self, "intensity", max(0.0, min(1.0, inten)))
        pace = str(self.pace or "normal").strip().lower()
        if pace not in ("slow", "normal", "fast"):
            pace = "normal"
        object.__setattr__(self, "pace", pace)

    def is_neutral(self) -> bool:
        return self.emotion == "neutral"

    def cache_key(self) -> str:
        """用于 TTS 缓存键的紧凑串；neutral 返回空串（== 无情绪）。"""
        if self.is_neutral():
            return ""
        return f"{self.emotion}:{self.intensity:.1f}:{self.pace}"


NEUTRAL = EmotionSpec()


# ── 情绪派生（纯函数）────────────────────────────────────────────────────────
# intent / 关系阶段 / CSAT 优先级最高；都没有时退化到文本线索；再没有 → neutral。
_INTENT_EMOTION = {
    # 关键词子串匹配（intent tag 词表各平台不同，用子串更鲁棒）
    "complaint": "empathetic", "投诉": "empathetic", "angry": "empathetic",
    "refund": "apologetic", "退款": "apologetic", "退货": "apologetic",
    "apolog": "apologetic", "道歉": "apologetic", "sorry": "apologetic",
    "greet": "warm", "问候": "warm", "hello": "warm", "打招呼": "warm",
    "thank": "warm", "感谢": "warm", "谢谢": "warm",
    "praise": "happy", "好评": "happy", "夸": "happy",
    "order": "happy", "下单": "happy", "购买": "happy", "成交": "excited",
    "farewell": "warm", "告别": "warm", "再见": "warm",
}

_TEXT_CUES = (
    # (子串元组, 情绪)；命中即取，顺序=优先级
    (("对不起", "不好意思", "抱歉", "sorry", "apolog"), "apologetic"),
    (("哈哈", "嘻嘻", "lol", "😄", "😂", "🤣", "笑死"), "playful"),
    (("谢谢", "感谢", "thank", "❤", "🥰", "么么"), "warm"),
    (("太好了", "棒", "恭喜", "🎉", "！！", "!!"), "excited"),
    (("难过", "伤心", "唉", "😢", "😭", "可惜"), "sad"),
)


def derive_emotion(
    *,
    intent: Optional[str] = None,
    rel_stage: Optional[str] = None,
    csat: Optional[float] = None,
    text: Optional[str] = None,
    default: str = "warm",
    persona: Optional[Dict[str, Any]] = None,
    peer_audio_emotion: Optional[Dict[str, Any]] = None,
) -> EmotionSpec:
    """从会话上下文派生情绪。任何脏输入都安全退化。

    优先级：CSAT 极差 → **对方声学语气**（听到难过/生气/恐惧就共情/安抚/沉稳回应）
    → intent 命中 → 文本线索 → 关系阶段微调 → 最后 ``default``。

    ``peer_audio_emotion``：上一条客户语音的音频情绪 dict（见 speech_emotion.map_audio_emotion），
    让「听到的语气」驱动「说出的语气」——**回应式**而非镜像（对方难过→我们温柔而非跟着难过）。
    """
    # 1) CSAT 极差（强信号）→ 共情安抚，盖过其他
    try:
        if csat is not None and float(csat) <= 2.0:
            return EmotionSpec("empathetic", intensity=0.8, pace="slow")
    except (TypeError, ValueError):
        pass

    emo: Optional[str] = None

    # 1.5) 对方声学语气（强信号，仅高置信生效）→ 回应式情绪。放在 intent/文本线索之前，
    # 因为「听到的真实语气」比「文字里的关键词」更贴近对方当下状态。
    if isinstance(peer_audio_emotion, dict) and peer_audio_emotion.get("confident"):
        try:
            from src.ai.speech_emotion import peer_emotion_to_reply
            _reply_emo = peer_emotion_to_reply(
                peer_audio_emotion.get("raw_label"),
                peer_audio_emotion.get("score") or 0.0,
                min_confidence=0.5,
            )
            if _reply_emo in EMOTIONS and _reply_emo != "neutral":
                _pace = "slow" if _reply_emo in ("empathetic", "calm") else "normal"
                return EmotionSpec(_reply_emo, intensity=0.78, pace=_pace)
        except Exception:
            pass

    # 2) intent 子串匹配
    it = str(intent or "").strip().lower()
    if it:
        for key, val in _INTENT_EMOTION.items():
            if key in it:
                emo = val
                break

    # 3) 文本线索
    if emo is None and text:
        t = str(text)
        tl = t.lower()
        for cues, val in _TEXT_CUES:
            if any(c in t or c in tl for c in cues):
                emo = val
                break

    # 4) 关系阶段微调（亲密阶段更暖更俏皮）
    rs = str(rel_stage or "").strip().lower()
    if emo is None:
        if rs in ("intimate", "close", "亲密", "lover", "好友", "friend"):
            emo = "playful"
        elif rs in ("new", "stranger", "陌生", "lead"):
            emo = "warm"

    # 5) 人设默认声线基调：让「目标人设」不仅换音色，也影响语气。
    if emo is None:
        emo = persona_default_emotion(persona)

    if emo is None:
        emo = default if default in EMOTIONS else "warm"

    # 强度：亲密关系 / 强信号略增
    intensity = 0.6
    if rs in ("intimate", "close", "亲密", "lover"):
        intensity = 0.75
    return EmotionSpec(emo, intensity=intensity)


def persona_default_emotion(persona: Optional[Dict[str, Any]]) -> Optional[str]:
    """Infer a stable TTS baseline from persona traits/style/tags.

    This is deliberately conservative: explicit conversation signals still win.
    The goal is to make cloned voices sound like the selected character even when
    the current reply text has no obvious emotion cue.
    """
    if not isinstance(persona, dict) or not persona:
        return None
    # Operator-pinned baseline wins (set via persona ``voice_profile.emotion``
    # or a top-level ``voice_emotion``). Makes "voice asset" assignment
    # deterministic instead of relying on keyword inference over free text.
    vp = persona.get("voice_profile")
    explicit = ""
    if isinstance(vp, dict):
        explicit = str(vp.get("emotion") or "").strip().lower()
    if not explicit:
        explicit = str(persona.get("voice_emotion") or "").strip().lower()
    if explicit in EMOTIONS and explicit != "neutral":
        return explicit
    parts = []
    for key in ("role", "background"):
        if persona.get(key):
            parts.append(str(persona.get(key)))
    for t in persona.get("tags") or []:
        parts.append(str(t))
    p = persona.get("personality") or {}
    if isinstance(p, dict):
        parts.extend(str(x) for x in (p.get("traits") or []))
        for key in ("style", "quirks"):
            if p.get(key):
                parts.append(str(p.get(key)))
    blob = " ".join(parts).lower()
    if not blob:
        return None
    if any(k in blob for k in ("活泼", "俏皮", "调皮", "爱笑", "热情", "外放", "playful")):
        return "playful"
    if any(k in blob for k in ("开心", "快乐", "乐观", "阳光", "happy")):
        return "happy"
    if any(k in blob for k in ("温暖", "体贴", "亲切", "温柔", "护士", "共情", "empathy")):
        return "warm"
    if any(k in blob for k in ("感性", "细腻", "文艺", "浪漫", "慢热")):
        return "calm"
    if any(k in blob for k in ("严谨", "专业", "金融", "顾问", "冷静", "克制", "成熟", "serious")):
        return "serious"
    return None


# ── 情绪 → 引擎控制映射 ───────────────────────────────────────────────────────
def coerce_emotion(value: Union[None, str, Dict[str, Any], EmotionSpec]) -> EmotionSpec:
    """把灵活输入（None/字符串/dict/EmotionSpec）规整成 EmotionSpec。"""
    if isinstance(value, EmotionSpec):
        return value
    if value is None:
        return NEUTRAL
    if isinstance(value, str):
        return EmotionSpec(value)
    if isinstance(value, dict):
        return EmotionSpec(
            emotion=str(value.get("emotion") or "neutral"),
            intensity=value.get("intensity", 0.6),
            pace=str(value.get("pace") or "normal"),
        )
    return NEUTRAL


def to_openai_instructions(spec: EmotionSpec, *, base: str = "") -> str:
    """OpenAI gpt-4o-mini-tts 的 ``instructions`` 自然语言指令。

    base 为人设/全局已配置的指令；情绪在其后追加（不覆盖运营显式设置）。
    neutral → 原样返回 base（无新增）。
    """
    base = str(base or "").strip()
    if spec.is_neutral():
        return base
    tone = _EMOTION_PROFILE[spec.emotion]["tone"]
    if not tone:
        return base
    degree = "强烈地" if spec.intensity >= 0.75 else ("略微" if spec.intensity <= 0.4 else "")
    pace_cn = {"slow": "语速放慢", "fast": "语速加快", "normal": ""}.get(spec.pace, "")
    parts = [f"用{degree}{tone}的语气说话"]
    if pace_cn:
        parts.append(pace_cn)
    instr = "；".join(parts)
    return f"{base}。{instr}" if base else instr


def emotion_tone_descriptor(spec: EmotionSpec) -> str:
    """情绪 → 中文语气**描述词**（如「俏皮、活泼、带点调侃」）；neutral/未知 → ""。

    供「文本系统提示」里描述人设语气基调（实时通话开场锚定）——与 ``to_*_instructions``
    的「指令句」不同，这里只给**形容词组**，由调用方包成完整句子。
    """
    if spec.is_neutral():
        return ""
    return str(_EMOTION_PROFILE.get(spec.emotion, {}).get("tone", "") or "")


def to_qwen_instructions(spec: EmotionSpec, *, base: str = "") -> str:
    """Qwen3-TTS / DashScope ``instructions`` 自然语言声音指令。

    Qwen3-TTS 与 OpenAI 一样消费自然语言「声音指令」——这是 **API 字段**，模型据此
    调整语气/语速，**绝不会被读出来**（零 garble 风险，故可默认开启）。复用同一套
    措辞逻辑。neutral → 原样返回 base（无新增）。
    """
    return to_openai_instructions(spec, base=base)


# 情绪 → fish_speech 内联情感标记（S2 Pro 文档词表，括号风格）。
# 仅取官方词表里的词，降低「不识别即读出」风险；仍建议 opt-in
# （见 voice_clone_lan.emotion_inline_tags），因不同 server build 支持度不一。
_FISH_MARKER: Dict[str, str] = {
    "neutral":    "",
    "warm":       "(comforting)",
    "happy":      "(joyful)",
    "excited":    "(excited)",
    "playful":    "(amused)",
    "empathetic": "(empathetic)",
    "apologetic": "(guilty)",
    "calm":       "(relaxed)",
    "sad":        "(sad)",
    "serious":    "(serious)",
}


def fish_marker(spec: EmotionSpec) -> str:
    """情绪 → fish_speech 内联标记（如 ``(joyful)``）；neutral/未知 → 空串。"""
    if spec.is_neutral():
        return ""
    return _FISH_MARKER.get(spec.emotion, "")


def to_fish_text(text: str, spec: EmotionSpec) -> str:
    """在文本前注入 fish_speech 情感标记（如 ``(joyful) 你好``）。

    neutral / 未知情绪 / 空文本 → 原文不变。标记被 S2 Pro 当情感提示消费；
    若 server 不支持该词，最坏情况是读出括号内容，故调用方应 opt-in。
    """
    t = str(text or "")
    if not t.strip():
        return t
    marker = fish_marker(spec)
    if not marker:
        return t
    return f"{marker} {t}"


def to_elevenlabs_text(text: str, spec: EmotionSpec) -> str:
    """ElevenLabs v3 内联音频标签：在文本前注入情绪标签（如 ``[warmly] 你好``）。

    neutral → 原文不变。标签用 v3 的小写方括号约定。
    """
    t = str(text or "")
    if spec.is_neutral() or not t.strip():
        return t
    tag = _EMOTION_PROFILE[spec.emotion]["el_tag"]
    if not tag:
        return t
    return f"[{tag}] {t}"


# ElevenLabs v3 voice_settings：(stability, style)。
# stability 调低 → 更听情绪标签 + 更大情感起伏；style 放大音色个性。
# 这是比内联标签更**可靠**的情感杠杆（标签依赖音色/上下文，settings 始终生效）。
_EL_SETTINGS: Dict[str, tuple] = {
    "neutral":    (0.50, 0.00),
    "warm":       (0.45, 0.25),
    "happy":      (0.35, 0.40),
    "excited":    (0.30, 0.55),
    "playful":    (0.35, 0.50),
    "empathetic": (0.40, 0.30),
    "apologetic": (0.45, 0.20),
    "calm":       (0.60, 0.10),
    "sad":        (0.40, 0.35),
    "serious":    (0.60, 0.10),
}


def elevenlabs_voice_settings(
    spec: EmotionSpec, *, similarity_boost: float = 0.75,
) -> Dict[str, Any]:
    """情绪 → ElevenLabs v3 ``voice_settings``（始终返回完整 dict，含 neutral 默认）。

    ``style`` 随 intensity 放大；``speed`` 由 pace 映射。``similarity_boost`` 控制
    与克隆音源的相似度（越高越像，但也放大原录音底噪），可由调用方覆盖。
    """
    base_stab, base_style = _EL_SETTINGS.get(spec.emotion, (0.50, 0.0))
    style = round(min(1.0, base_style * (0.5 + spec.intensity)), 2)
    stability = round(max(0.0, min(1.0, base_stab)), 2)
    sim = round(max(0.0, min(1.0, float(similarity_boost))), 2)
    out: Dict[str, Any] = {
        "stability": stability,
        "similarity_boost": sim,
        "style": style,
        "use_speaker_boost": True,
    }
    speed = {"slow": 0.92, "fast": 1.08, "normal": 1.0}.get(spec.pace, 1.0)
    if speed != 1.0:
        out["speed"] = speed
    return out


# 情绪 → CosyVoice3(AvatarHub 7852) emotion 标签。服务端词表：
# neutral/happy/sad/angry/fearful/surprised/disgusted/gentle/excited/calm/serious。
# 系统词表中的「暖/共情类」统一映射 gentle；angry/fearful 等负面标签**刻意不映射**
# ——AI 角色对用户发火/恐惧不是本产品语气。
_COSYVOICE_EMOTION: Dict[str, str] = {
    "neutral":    "neutral",
    "warm":       "gentle",
    "happy":      "happy",
    "excited":    "excited",
    "playful":    "happy",
    "empathetic": "gentle",
    "apologetic": "calm",
    "calm":       "calm",
    "sad":        "sad",
    "serious":    "serious",
}

# 强情绪阈值：intensity ≥ 此值才切 7852 的 instruct2 情感路径。
# ⚠ 音色保真机制（2026-07-13 事故复盘）：7852 /v1/tts/clone 收到非 neutral emotion
# 时走 inference_instruct2——该路径**完全忽略 reference_text 逐字稿**，音色相似度
# 显著下降（用户原话："没用克隆声，太像豆包 AI"）。而 neutral+逐字稿走
# inference_zero_shot=音色最像。故弱情绪（日常闲聊 intensity≈0.6）一律走保真路径，
# 情绪表达交给**副语言标记+变速**（两者在保真路径同样生效，真机 A/B 验证）；
# 只有强情绪（真难过/真兴奋/亲密场景 ≥0.7）才值得用音色换表现力。
STRONG_EMOTION_THRESHOLD = 0.7


def to_cosyvoice_emotion(
    spec: EmotionSpec, *, default: str = "neutral",
    strong_threshold: float = STRONG_EMOTION_THRESHOLD,
) -> str:
    """情绪 → CosyVoice3 emotion 标签（7852 /v1/tts/clone 的 ``emotion`` 字段）。

    音色保真优先：neutral / 弱情绪（intensity < strong_threshold）→ "neutral"
    （服务端走 zero_shot+逐字稿，音色最像；情绪由副语言标记+speed 承担）；
    强情绪 → 对应标签（instruct2 情感路径，用音色换表现力）。纯函数、防御式。
    """
    d = str(default or "neutral")
    if spec is None or spec.is_neutral():
        return d
    try:
        if float(spec.intensity) < float(strong_threshold):
            return "neutral"
    except (TypeError, ValueError):
        return "neutral"
    return _COSYVOICE_EMOTION.get(spec.emotion, d)


# 情绪 → 基础语速（活人感：难过的人说话慢、兴奋的人说话快；1.0=原速）。
# 幅度保守（±8% 内）——过度变速会露「变速处理」痕迹，且真机实测 0.93 以下
# 咬字开始模糊（STT 把「失落」听成「示弱」），保真优先收窄下限。
_EMOTION_SPEED: Dict[str, float] = {
    "sad":        0.92,
    "empathetic": 0.94,
    "apologetic": 0.96,
    "calm":       0.96,
    "warm":       1.0,
    "serious":    0.98,
    "happy":      1.04,
    "playful":    1.05,
    "excited":    1.08,
}


def is_quiet_hour(hour: Optional[int]) -> bool:
    """深夜档判定（23–6 点）。``hour=None``＝不启用夜间调制。纯函数。"""
    if hour is None:
        return False
    try:
        h = int(hour)
    except (TypeError, ValueError):
        return False
    return h >= 23 or h < 7


def cosyvoice_speed(spec: EmotionSpec, *, hour: Optional[int] = None) -> float:
    """情绪(+pace) → CosyVoice3 ``speed``。

    活人感设计：情绪本身携带默认速度曲线（sad 0.92 / excited 1.08），
    ``pace`` 在其上相乘微调（slow ×0.95 / fast ×1.05）；neutral 恒 1.0。
    ``hour`` 传入当前小时（调用方取 now）→ 深夜(23–6) 整体 ×0.96
    ——真人深夜发语音会不自觉变轻变慢（「悄悄话」感），neutral 也生效。
    结果限幅 [0.90, 1.12]——下限依真机咬字实测收紧（过慢=转写级含糊）。
    """
    night_mul = 0.96 if is_quiet_hour(hour) else 1.0
    if spec is None or spec.is_neutral():
        return round(min(1.12, max(0.90, 1.0 * night_mul)), 3)
    base = _EMOTION_SPEED.get(spec.emotion, 1.0)
    pace_mul = {"slow": 0.95, "fast": 1.05}.get(spec.pace, 1.0)
    return round(min(1.12, max(0.90, base * pace_mul * night_mul)), 3)


# ── 情绪分库参考音（①，2026-07-14 夜活人感批）────────────────────────────────
# zero_shot 的本质是模仿参考音的「说话状态」：开心的参考音合出来每句自带笑意，
# 比 emotion 标签自然得多（且不掉 instruct2 音色漂移路径）。人设配
# ``voice_profile.reference_audio_by_emotion: {happy: p1.wav, sad: p2.wav, ...}``
# 后，强情绪轮次自动换用对应「状态」的参考音，emotion 标签归 neutral 走纯保真。
_EMOTION_REF_ALIASES: Dict[str, Tuple[str, ...]] = {
    # 库键 → 可吸附的 EmotionSpec 情绪（精确命中优先于别名组）
    "happy":  ("happy", "excited", "playful"),
    "sad":    ("sad", "empathetic"),
    "calm":   ("calm", "serious", "apologetic"),
    "warm":   ("warm",),
}


def pick_emotion_reference(
    voice_profile: Optional[Dict[str, Any]],
    spec: Optional[EmotionSpec],
    *,
    threshold: float = 0.5,
) -> Optional[Tuple[str, str]]:
    """按当轮情绪从人设情绪参考音库选 ref。返回 ``(ref_path, 库键)``；不适用 → None。

    选择规则（保守）：neutral/弱情绪(<threshold) 不切（默认参考音=保真主路）；
    精确键命中优先，其次别名组；文件不存在 → None（回落默认，绝不报错）。纯函数+存在性检查。
    """
    vp = voice_profile or {}
    lib = vp.get("reference_audio_by_emotion")
    if not isinstance(lib, dict) or not lib:
        return None
    if spec is None or spec.is_neutral():
        return None
    try:
        if float(spec.intensity) < float(threshold):
            return None
    except (TypeError, ValueError):
        return None
    emo = str(spec.emotion or "").strip().lower()
    if not emo:
        return None

    def _valid(key: str) -> Optional[Tuple[str, str]]:
        p = str(lib.get(key) or "").strip()
        if p and Path(p).is_file():
            return p, key
        return None

    hit = _valid(emo)                       # 精确命中（运营可配任意键名）
    if hit:
        return hit
    for key, members in _EMOTION_REF_ALIASES.items():
        if emo in members:
            hit = _valid(key)
            if hit:
                return hit
    return None


# ── 副语言标记注入（CosyVoice3 原生 [breath]/[laughter]/[sigh] 内联标记）────
# 活人感的最后一公里：真人说话有气口、叹气、笑场——平滑的「播报腔」正是 AI 感
# 的来源。CosyVoice3 原生支持内联副语言标记（官方论文 §2.5：5000h 指令数据，
# [laughter]/[breath] 等 vocal bursts 在 tokenizer 层消费，**绝不会被读出**——
# 2026-07-13 真机 A/B：[breath]/[laughter]/[sigh]/<strong> 四种标记 STT 回转
# 全部零 garble，样本存 tmp_tts_preview/paraling/）。
#
# 注入原则（宁少勿多——副语言是味精，超量立刻假）：
#   - **确定性**：crc32(文本) 决定注入与否/位置，同文本同结果（TTS 缓存安全）；
#   - 每条消息至多 max_marks 个标记（默认 2）；
#   - 情绪对路才注入：sad/empathetic → 叹气/气口；playful/happy/excited → 笑声
#     （且只在文本本身有笑点信号时才笑——没笑点硬笑是恐怖谷）；
#     serious/apologetic/neutral → 不注入（庄重场合叹气/笑都是事故）；
#   - intensity 联动：强度越高注入概率越高（0.4 以下基本不注入）。
_SIGH_LEAD_RE = None      # 句首叹词（唉/哎/哦/呜/嗯 开头）
_LAUGH_CUE_RE = None      # 笑点信号（哈哈/嘻嘻/太逗/笑死/绝了…）
_BREATH_SPLIT_RE = None   # 气口候选位置（逗号/顿号后）
_CRY_LEAD_RE = None       # 句首哭声拟声词（呜呜/嘤嘤/哇哇，2+ 连字）

# 各情绪的注入配方：(句首标记, 句首概率基线, 笑点后标记, 气口标记, 气口概率基线)
_PARALING_RECIPE: Dict[str, Dict[str, Any]] = {
    "sad":        {"lead": "[sigh]",   "lead_p": 0.75, "breath": "[breath]", "breath_p": 0.5},
    "empathetic": {"lead": "[breath]", "lead_p": 0.5,  "breath": "[breath]", "breath_p": 0.4},
    "calm":       {"lead": "",         "lead_p": 0.0,  "breath": "[breath]", "breath_p": 0.3},
    "warm":       {"lead": "",         "lead_p": 0.0,  "breath": "[breath]", "breath_p": 0.25},
    "playful":    {"laugh": "[laughter]", "laugh_p": 0.9},
    "happy":      {"laugh": "[laughter]", "laugh_p": 0.85},
    "excited":    {"laugh": "[laughter]", "laugh_p": 0.7},
}


def _paraling_res() -> tuple:
    """惰性编译正则（模块导入零开销）。"""
    global _SIGH_LEAD_RE, _LAUGH_CUE_RE, _BREATH_SPLIT_RE, _CRY_LEAD_RE
    if _SIGH_LEAD_RE is None:
        import re
        # 长叹词在前（正则交替按序匹配）：防「呜呜」被单字「呜」截断成「呜[sigh]呜」
        _SIGH_LEAD_RE = re.compile(r"^(哎呀|呜呜|唉|哎|呜|嗯|哦|唔)")
        _LAUGH_CUE_RE = re.compile(
            r"(哈哈+|嘻嘻+|嘿嘿+|噗+|太逗|笑死|好好笑|太好笑|绝了|太搞笑|好搞笑)")
        _BREATH_SPLIT_RE = re.compile(r"[，,]")
        # 哭声拟声词（≥2 连字 + 可选停顿符）：TTS 念不稳（真机 STT 把「呜呜」
        # 听成「喂鱼」），且文字拟声本就是副语言——换成真叹气声更拟人。
        _CRY_LEAD_RE = re.compile(r"^(呜{2,}|嘤{2,}|哇{2,})[，,、\s]*")
    return _SIGH_LEAD_RE, _LAUGH_CUE_RE, _BREATH_SPLIT_RE, _CRY_LEAD_RE


# <strong> 重点词强调（③）：程度副词/强调词——真人说「真的超好吃」会重读「超」。
# 仅 excited/serious（表达欲/严肃强调场景）注入，每句 ≤1 个，占 marks 额度。
_STRONG_CUE_RE = None


def _strong_re():
    global _STRONG_CUE_RE
    if _STRONG_CUE_RE is None:
        import re
        # 程度副词 + 其修饰词（1-4 字）：「超好吃」「真的很想你」「特别开心」
        _STRONG_CUE_RE = re.compile(
            r"(真的|特别|超级|非常|绝对|超|太|最|好)"
            r"([\u4e00-\u9fff]{1,4}?)(?=[，,。.！!？?、的了呢呀啦]|$)")
    return _STRONG_CUE_RE


def inject_paralinguistic(
    text: str, spec: EmotionSpec, *, max_marks: int = 2,
) -> str:
    """按情绪把副语言标记注入待合成文本。纯函数、确定性、防御式。

    返回注入后的文本；不适用（空文本/超额）→ 原文不变。
    标记语义（CosyVoice3 tokenizer 层消费，不读出）：
      - ``[sigh]`` 句首＝低落开场的叹气；``[breath]`` ＝换气/轻叹（逗号后）；
      - ``[laughter]`` ＝笑声，只跟在**文本自带的笑点**（哈哈/太逗…）后面；
      - ``<strong>词</strong>`` ＝重点词重读（excited/serious，每句 ≤1）；
      - 通用长句呼吸（②）：≥22 字且 ≥2 个逗号的长句在中部换气——**任何情绪
        （含 neutral/serious）**都适用，呼吸是生理不是情绪表达。
    """
    t = str(text or "")
    if not t.strip() or max_marks <= 0:
        return t
    if any(m in t for m in ("[sigh]", "[breath]", "[laughter]", "<strong>")):
        return t  # 上游（LLM/运营）已手工标注 → 尊重，不叠加

    import zlib
    seed = zlib.crc32(t.encode("utf-8"))
    _spec = spec if spec is not None else NEUTRAL
    recipe = _PARALING_RECIPE.get(_spec.emotion) or {}
    # intensity 缩放：0.6 为基准倍率 1.0；0.4 以下急剧衰减，1.0 时 ×1.33
    inten = float(_spec.intensity if _spec.intensity is not None else 0.6)
    scale = max(0.0, (inten - 0.25)) / 0.35 if inten < 0.6 else inten / 0.6

    def _hit(salt: int, p: float, *, sc: float = None) -> bool:  # type: ignore[assignment]
        if p <= 0:
            return False
        s = scale if sc is None else sc
        return ((seed >> salt) % 100) < int(min(0.95, p * s) * 100)

    sigh_re, laugh_re, breath_re, cry_re = _paraling_res()
    marks = 0
    out = t
    emo = _spec.emotion if not _spec.is_neutral() else "neutral"

    # 0) 哭声拟声词规整（sad/empathetic 无条件，不占概率位）：句首「呜呜/嘤嘤/哇哇」
    # → [sigh]（TTS 念拟声词发音不稳=直接的假；真叹气声既稳又更像活人在哽咽）。
    if emo in ("sad", "empathetic"):
        m = cry_re.match(out)
        if m and marks < max_marks:
            out = "[sigh]" + out[m.end():]
            marks += 1

    # 1) 笑点后插笑声（playful/happy/excited）——只笑真笑点
    laugh_tag = recipe.get("laugh")
    if laugh_tag and marks < max_marks and _hit(3, recipe.get("laugh_p", 0)):
        m = laugh_re.search(out)
        if m:
            out = out[:m.end()] + laugh_tag + out[m.end():]
            marks += 1

    # 2) 句首叹气/气口（sad/empathetic）——有叹词跟在叹词后，无叹词直接开场；
    # 句首已有标记（如哭声规整产物）→ 跳过，防「[sigh][sigh]」叠加。
    lead_tag = recipe.get("lead")
    if (lead_tag and marks < max_marks and not out.startswith("[")
            and _hit(7, recipe.get("lead_p", 0))):
        m = sigh_re.match(out)
        if m:
            out = out[:m.end()] + lead_tag + out[m.end():]
        else:
            out = lead_tag + out
        marks += 1

    # 3) 逗号后气口（情绪配方档：sad/empathetic/calm/warm 的中长句）
    breath_tag = recipe.get("breath")
    breath_done = False
    if breath_tag and marks < max_marks and len(out) >= 14 \
            and _hit(11, recipe.get("breath_p", 0)):
        commas = [m.end() for m in breath_re.finditer(out)]
        if commas:
            pos = commas[(seed >> 15) % len(commas)]
            out = out[:pos] + breath_tag + out[pos:]
            marks += 1
            breath_done = True

    # 4) 通用长句呼吸（②，2026-07-14）：真人 22 字以上必换气——**与情绪无关**
    # （neutral/serious/happy 长句同样要呼吸）。高概率固定档（呼吸是生理不是
    # 情绪表达，不吃 intensity 缩放），选中部逗号；已插过配方气口则不叠。
    if (not breath_done and marks < max_marks and len(out) >= 22
            and _hit(19, 0.8, sc=1.0)):
        commas = [m.end() for m in breath_re.finditer(out)]
        if len(commas) >= 2:
            pos = commas[len(commas) // 2]      # 取中部逗号（呼吸落在语义段间）
            out = out[:pos] + "[breath]" + out[pos:]
            marks += 1

    # 5) 重点词重读（③）：excited/serious 且强度≥0.5——程度副词+修饰词包 <strong>
    # （「真的<strong>超好吃</strong>」）。每句 ≤1，宁缺勿滥。
    if (emo in ("excited", "serious") and inten >= 0.5 and marks < max_marks
            and _hit(23, 0.7)):
        m = _strong_re().search(out)
        if m and "[" not in m.group(0):
            out = (out[:m.start()] + "<strong>" + m.group(0) + "</strong>"
                   + out[m.end():])
            marks += 1

    return out


# ── 动态 instruct 模板库（CosyVoice3 /v1/tts/instruct 消费）─────────────────
# 比 emotion 标签更细腻的自然语言语气指令；每情绪 2-3 个「语气内核」变体轮换，
# 避免同一情绪永远一个味。真机 A/B 已验证（2026-07-12）：instruct 通道中文合成
# STT 回转完全正确、语气可控。变体选择用 crc32(文本) 确定性取模——同一文本永远
# 选同一变体（Python hash() 进程间随机化，不可用），TTS 缓存键因此稳定。
# 内核不含「用…的语气说」外壳——由 to_cosyvoice_instruct 统一组装，便于与
# 人设声线底色（instruct_style）自然复合（「用<底色>、<内核>的语气说」）。
_COSYVOICE_INSTRUCT_BANK: Dict[str, tuple] = {
    "warm": (
        "温暖亲切、像跟很熟的人聊天",
        "温柔贴心、带着浅浅笑意",
        "温暖中带一点点撒娇",
    ),
    "happy": (
        "开心轻快、藏不住笑意",
        "愉快活泼、心情很好",
    ),
    "excited": (
        "兴奋雀跃、迫不及待分享好消息",
        "特别激动开心、语调上扬",
    ),
    "playful": (
        "俏皮活泼、带点小得意",
        "调皮撒娇、逗对方开心",
        "轻松俏皮、带着笑闹感",
    ),
    "empathetic": (
        "心疼对方、轻声安抚",
        "温柔共情、像陪在身边一样",
    ),
    "apologetic": (
        "诚恳愧疚、放低姿态",
        "小心翼翼、带着歉意",
    ),
    "calm": (
        "平静舒缓、让人安心",
        "轻松从容、慢慢聊天",
    ),
    "sad": (
        "低落委屈、轻轻叹气",
        "难过失落、声音放轻",
    ),
    "serious": (
        "认真郑重、一字一句清晰",
        "严肃可靠、值得信赖",
    ),
}

# 人设声线底色词表（voice_profile.instruct_style 的合法值 → 语气底色描述）。
# 底色与当轮情绪内核复合成一句指令，让同一情绪在不同人设口中有不同「人味」。
# 收词保守：只收 7852 实测能稳定表达的风格词；未知值忽略（等同不配）。
_INSTRUCT_STYLES: Dict[str, str] = {
    "撒娇": "撒娇黏人",
    "俏皮": "活泼俏皮",
    "温柔": "轻声温柔",
    "御姐": "成熟慵懒",
    "沉稳": "沉稳可靠",
    "清冷": "清冷淡然",
    "阳光": "阳光爽朗",
}


def to_cosyvoice_instruct(
    spec: EmotionSpec, *, seed_text: str = "", base: str = "", style: str = "",
) -> str:
    """情绪(×人设底色) → CosyVoice3 自由语气指令（动态 instruct 通道）。

    - ``seed_text``：待合成文本，用于**确定性**变体轮换（crc32 取模）——同文本
      同指令，缓存友好；不同文本在同情绪下语气有自然变化。
    - ``base``：人设显式配置的静态 instruct；非空时直接返回 base（运营显式配置
      永远最高优先，动态库不覆盖）。
    - ``style``：人设声线底色（``voice_profile.instruct_style``，词表见
      ``_INSTRUCT_STYLES``）；与情绪内核复合为「用<底色>、<内核>的语气说」——
      同是 warm，撒娇底色与沉稳底色念出来是两个人。未知值忽略。
    - intensity ≥0.75 追加「情绪更饱满」/ ≤0.4 追加「情绪收着点」；pace 追加语速。
    - neutral / 未知情绪 → ""（调用方回落 emotion 标签通道）。纯函数。
    """
    base = str(base or "").strip()
    if base:
        return base
    if spec is None or spec.is_neutral():
        return ""
    variants = _COSYVOICE_INSTRUCT_BANK.get(spec.emotion)
    if not variants:
        return ""
    import zlib
    idx = zlib.crc32(str(seed_text or "").encode("utf-8")) % len(variants)
    core = variants[idx]
    style_tone = _INSTRUCT_STYLES.get(str(style or "").strip(), "")
    # 底色与本轮内核语义重叠（任一 2 字词已在内核里，如 底色「活泼俏皮」×内核
    # 「俏皮活泼」）→ 跳过底色，避免「用活泼俏皮、俏皮活泼…」式冗余指令。
    if style_tone and any(
        style_tone[i:i + 2] in core for i in range(len(style_tone) - 1)
    ):
        style_tone = ""
    instr = (
        f"用{style_tone}、{core}的语气说" if style_tone else f"用{core}的语气说")
    if spec.intensity >= 0.75:
        instr += "，情绪饱满一些"
    elif spec.intensity <= 0.4:
        instr += "，情绪收着一点"
    pace_part = {"slow": "，语速放慢", "fast": "，语速稍快"}.get(spec.pace, "")
    return instr + pace_part


def edge_prosody(spec: EmotionSpec) -> Dict[str, str]:
    """edge_tts 的 rate/pitch 字符串（按 intensity 缩放）。

    返回 ``{"rate": "+8%", "pitch": "+4Hz"}`` 形式；neutral → 空 dict（不调）。
    """
    if spec.is_neutral():
        return {}
    prof = _EMOTION_PROFILE[spec.emotion]
    scale = 0.5 + spec.intensity  # 0.5..1.5
    rate = int(round(prof["rate"] * scale))
    pitch = int(round(prof["pitch"] * scale))
    out: Dict[str, str] = {}
    if rate:
        out["rate"] = f"{rate:+d}%"
    if pitch:
        out["pitch"] = f"{pitch:+d}Hz"
    return out


__all__ = [
    "EmotionSpec", "NEUTRAL", "EMOTIONS",
    "derive_emotion", "coerce_emotion", "persona_default_emotion",
    "emotion_tone_descriptor",
    "to_openai_instructions", "to_qwen_instructions",
    "to_elevenlabs_text", "elevenlabs_voice_settings",
    "fish_marker", "to_fish_text",
    "to_cosyvoice_emotion", "cosyvoice_speed", "to_cosyvoice_instruct",
    "inject_paralinguistic",
    "edge_prosody",
]
