"""Stage A：陪伴「形象照/自拍」引擎（对标星野/Talkie/Replika 招牌能力）。

把变现目录里早已定义、却**零交付代码**的付费项 `exclusive_album`（专属相册）真正"通电"：
用户在对话里要照片 → 按关系等级 + 付费权益判准入 → 生成在 persona 一致的形象照 → 发出；
够不着的（关系浅）温柔搪塞，未解锁的给软付费引导（驱动 exclusive_album 转化）。

本模块是**纯逻辑 + 软失败 provider 骨架**（镜像 `tts_pipeline` 范式）：意图识别 / 提示词构造 /
准入决策都是可单测纯函数；图像 provider 默认 `disabled`（不接真模型零行为），接 openai images /
本地命令模板（如 ComfyUI/SD 推理脚本）后才真正出图。绝不抛——任何失败退回文字陪伴。

安全：提示词强制 SFW 约束（成人/暴露内容硬约束在 prompt 层，配合 persona_guard/wellbeing）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from src.utils.monetization import feature_allowed

logger = logging.getLogger(__name__)

# 付费项 id（变现目录 items.exclusive_album）；自拍超出免费额度后据此判拥有/引导解锁。
SELFIE_FEATURE = "exclusive_album"

# 意图关键词（多语，刻意保守——须明确指向"对方/AI 的样子/照片"，避免误命中用户自述照片）。
_REQUEST_MARKERS = (
    "自拍", "你的照片", "你的相片", "你的样子", "你长什么样", "你长啥样",
    "拍张照", "拍张自拍", "拍一张", "发张照片", "发张自拍", "发张图", "发个自拍",
    "看看你", "想看你", "你的照", "来张照片", "给我看看你", "你的写真", "你的近照",
    "你近照", "妳近照", "妳的近照",
    # 2026-07-14 真机验收漏报补齐："再给我发一遍新照片，不要老照片"一个 marker 都没
    # 命中 → AI 嘴上答应发图实际装死。"新照片/近照"是强索图词，单独出现基本都在要图
    # （客户谈自己的新照片被误发一张自拍属可接受的便宜错误，方向反了也不崩人设）。
    "新照片", "近照", "新自拍", "新的自拍", "最新的照片", "现在的照片",
    "再来一张", "再发一张", "再拍一张", "再來一張", "再發一張", "再拍一張",
    # 繁体 / 港台常见写法（对方多为繁体输入，简体 marker 匹配不到 → 补齐同义）
    "發張照片", "發個照片", "發張自拍", "發個自拍", "發張圖", "傳張照片", "傳個照片",
    "拍個照片", "來張照片", "給我看看你", "給我看看妳", "看看妳", "想看妳",
    "你的樣子", "妳的照片", "妳的相片", "妳的樣子", "你長什麼樣", "你長啥樣",
    "妳長什麼樣", "你的寫真", "你的近照",
    "selfie", "photo of you", "pic of you", "picture of you", "send a pic",
    "send me a pic", "send a photo", "show me you", "show me your face",
    "what do you look like", "your photo", "your picture", "see your face",
    "your recent photo", "recent photo of you", "latest photo of you",
    "new photo", "new pic", "another photo", "another pic", "one more photo",
    "one more pic",
)

# 反向护栏：明确指向"对方做/煮/买的东西"的照片（"你煮的…拍张照给我看"）属**对话临时要图**
# （上下文要图 = 后续 Stage B），不是"人设本人自拍" → 命中则不当作 selfie，避免误发人设照片。
_OBJECT_PHOTO_MARKERS = (
    "你煮的", "妳煮的", "你做的", "妳做的", "你买的", "你買的",
    "你拍的", "你點的", "你点的", "你種的", "你种的", "你養的", "你养的",
    "你寫的", "你写的", "你画的", "你畫的",
)


def detect_selfie_request(text: str) -> bool:
    """是否在向 AI 索要形象照/自拍（多语、保守，含繁体）。

    反向护栏：请求明确指向"对方做/煮/买的东西"的照片（``你煮的…拍张照``）→ 返回 False，
    交由上下文要图路径处理，避免把"拍下你煮的面"误当成"发一张你的自拍"。
    """
    t = str(text or "").strip().lower()
    if not t or len(t) > 200:  # 超长多半是叙述而非索图
        return False
    if any(m in t for m in _OBJECT_PHOTO_MARKERS):
        return False
    return any(m in t for m in _REQUEST_MARKERS)


def _persona_visual(persona: Any) -> str:
    """从 persona（dict/str）抽取**真实外貌描述**（不含 name 兜底）；缺则空串。

    str 形态护栏：调用方（skill_manager）在 persona dict 拿不到时会回传**人设名字符串**
    （如"林小雨"）——短且无分隔符的纯 CJK 串按名字论、不当外貌描述（名字对生图模型是
    噪声，是 2026-07-13 狗图事故的帮凶之一）；真正的中文外貌描述通常含逗号/空格。
    """
    if isinstance(persona, str):
        s = persona.strip()
        if (_has_cjk(s) and len(s) <= 10
                and not any(sep in s for sep in (" ", ",", "，", "、", ";", "；"))):
            return ""
        return s
    if not isinstance(persona, dict):
        return ""
    for k in ("appearance", "visual", "look", "self_image_desc", "description"):
        v = str(persona.get(k) or "").strip()
        if v:
            return v
    return ""


def _persona_name(persona: Any) -> str:
    return str(persona.get("name") or "").strip() if isinstance(persona, dict) else ""


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in str(s or ""))


def _persona_gender_word(persona: Any) -> str:
    """从 persona 推断主体性别词（woman/man）；推不出返回空串。gender 字段优先，tags 兜底。"""
    if not isinstance(persona, dict):
        return ""
    g = str(persona.get("gender") or "").strip().lower()
    if g in ("female", "f", "woman", "女", "女性"):
        return "woman"
    if g in ("male", "m", "man", "男", "男性"):
        return "man"
    tags = {str(t).strip() for t in (persona.get("tags") or [])}
    if tags & {"女性", "女"}:
        return "woman"
    if tags & {"男性", "男"}:
        return "man"
    return ""


def _persona_smart_base(persona: Any) -> str:
    """persona 无外貌字段时，从结构化字段（gender/age/中文名）推一个**明确是人类**的主体描述。

    事故背景（2026-07-13）：旧兜底 ``a warm, friendly companion named 林小雨`` 中
    "companion" 在英文生图语料里强关联 companion animal（陪伴犬），且中文名对
    FLUX 的 CLIP/T5 编码器无任何约束 → 实际生成了一条穿黄毛衣的狗发给客户。
    因此这里**绝不把非拉丁名字塞进 prompt**，只输出 "a 22-year-old East Asian woman"
    这类模型能硬约束的人类主体描述。
    """
    if not isinstance(persona, dict):
        return ""
    subject = _persona_gender_word(persona) or "person"
    eth = "East Asian " if _has_cjk(_persona_name(persona)) else ""
    try:
        age_i = int(persona.get("age") or 0)
    except Exception:
        age_i = 0
    if 5 <= age_i <= 100:
        return f"a {age_i}-year-old {eth}{subject}, warm friendly expression"
    return f"a young {eth}{subject}, warm friendly expression"


# ── 「真人感」多样性池（治头位置/表情千篇一律）───────────────────────────────
# 背景：PuLID 从基准正面照锁脸 + 固定种子 + prompt 恒为「looking at the camera」
# 三重叠加 → 每张构图/头部朝向/表情雷同，像证件照连拍。这里提供**可轮换**的取景/
# 头部姿态/视线/表情/写实质感词池，由 ``selfie_variety`` 按 salt 确定性组合注入
# prompt。**刻意保守 SFW**：只收景别/角度/神态/生活化质感，不收衣着/体态类（那类
# 易滑向擦边，交由 SFW 硬约束）。保留「solo, one person」单人锚（防多人/非人跑偏，
# 狗图事故教训），只让「看镜头」这一条随池轮换。
_VARIETY_FRAMING = (
    "close-up selfie", "half-body shot", "waist-up framing",
    "candid photo with slightly off-center framing", "mirror selfie",
    "arm's-length selfie", "cozy indoor snapshot",
)
_VARIETY_HEAD = (
    "head tilted slightly", "three-quarter face angle", "gentle side profile",
    "facing forward naturally", "chin lifted a little", "leaning cheek on one hand",
)
_VARIETY_GAZE = (
    "looking at the camera", "looking away to the side", "glancing off-frame",
    "eyes softly downcast", "looking back over the shoulder",
    "gazing into the distance",
)
_VARIETY_EXPR = (
    "soft natural smile", "cheerful laugh with teeth showing",
    "relaxed calm expression", "playful little smirk",
    "slightly surprised, eyebrows raised", "warm gentle smile",
    "candid mid-laugh, eyes crinkled",
)
_VARIETY_REALISM = (
    "casual smartphone snapshot", "natural skin texture with subtle imperfections",
    "amateur candid photo, unposed", "soft phone-camera look with faint grain",
    "everyday casual photo, slightly imperfect focus",
)


def selfie_variety(salt: Any) -> Dict[str, str]:
    """按 ``salt`` 确定性地从取景/头部姿态/视线/表情/写实池各取一项（纯函数）。

    各池用不同的整除步长错开轮换（避免五项锁步一起变），同 salt 恒定可复现、
    缓存友好。返回 ``{framing, head, gaze, expr, realism}``；``build_selfie_prompt``
    据此拼多样性修饰词。非数字/异常 salt 归一为 0（返回各池首项）。
    """
    try:
        s = abs(int(salt))
    except Exception:
        s = 0
    return {
        "framing": _VARIETY_FRAMING[s % len(_VARIETY_FRAMING)],
        "head": _VARIETY_HEAD[(s // 7) % len(_VARIETY_HEAD)],
        "gaze": _VARIETY_GAZE[(s // 13) % len(_VARIETY_GAZE)],
        "expr": _VARIETY_EXPR[(s // 17) % len(_VARIETY_EXPR)],
        "realism": _VARIETY_REALISM[(s // 23) % len(_VARIETY_REALISM)],
    }


def resolve_variety_salt(scfg: Any, *, enabled_default: bool = False) -> Optional[int]:
    """按 ``companion.selfie.variety.enabled`` 决定本次出图的多样性 salt（治千篇一律）。

    返回随机 int（每次发送不同 → 姿态/表情/构图各异）或 None（关闭 → 旧行为）。
    默认关（``enabled_default=False``），线上经 overlay opt-in。软失败返回 None（不阻断出图）。
    调用方把它同时喂 ``build_selfie_prompt(variety_salt=)`` 与
    ``stable_selfie_seed(key, salt=)``，使 prompt 与底噪一起变化。
    """
    try:
        vc = (scfg or {}).get("variety") if isinstance(scfg, dict) else None
        on = (bool(vc.get("enabled", enabled_default)) if isinstance(vc, dict)
              else bool(enabled_default))
        if not on:
            return None
        return random.randint(0, 2 ** 30)
    except Exception:
        return None


# 机器独占的角色 LoRA 注册表（训练→选优后由 persona_lora_eval --write-registry 原子写入；
# 用 JSON 而非改人工带注释的 YAML → 零风险不丢注释）。resolve 作「人设字段 > 注册表 > 全局」
# 中间优先级读取（mtime 缓存，出图路径低频调用零负担）。
DEFAULT_LORA_REGISTRY = "config/persona_lora.json"
_LORA_REG_LOCK = threading.Lock()
_LORA_REG_CACHE: Dict[str, Any] = {"path": None, "mtime": -1.0, "data": {}}


def _persona_key(persona: Any) -> str:
    """人设注册表键：dict 取 id/persona_id/name；str（外貌描述兜底）原样（基本不会命中注册表）。"""
    if isinstance(persona, dict):
        return str(persona.get("id") or persona.get("persona_id")
                   or persona.get("name") or "").strip()
    return str(persona or "").strip()


def load_lora_registry(path: Any = None) -> Dict[str, Any]:
    """读角色 LoRA 注册表 JSON（mtime 缓存；不存在/损坏 → {}）。纯读、绝不抛。"""
    p = str(path or DEFAULT_LORA_REGISTRY)
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    with _LORA_REG_LOCK:
        if _LORA_REG_CACHE["path"] == p and _LORA_REG_CACHE["mtime"] == mt:
            return _LORA_REG_CACHE["data"]
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, dict) else {}
        except Exception:
            data = {}
        _LORA_REG_CACHE.update(path=p, mtime=mt, data=data)
        return data


def resolve_persona_lora(
    persona: Any, scfg: Any, *, registry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """解析该人设的**角色 LoRA spec**：``{file, weight, trigger}``。

    多人设各训各的 LoRA（不同文件/触发词），而出图 ``command_args`` 只有一条 → 靠这里按
    persona 解析、经 ``{lora}``/``{lora_weight}`` 占位与 prompt 触发词注入。
    **三层优先级**：① persona dict 的 ``lora_file``/``lora_trigger``/``lora_weight``
    （profiles_runtime.yaml 人工指定，最高）→ ② 注册表 ``config/persona_lora.json`` 的
    ``<pid>`` 项（训练选优后机器写回，deploy 免手改）→ ③ ``scfg['lora']`` 全局块
    （单人设/统一 LoRA 兜底）。``file`` 空＝不挂 LoRA（回落 PuLID）。字段级合并（各键独立取最高层）。
    ``registry`` 显式传入则用之（测试/批量用），否则自动加载默认注册表（mtime 缓存）。
    """
    lcfg = (scfg or {}).get("lora") if isinstance(scfg, dict) else None
    lcfg = lcfg if isinstance(lcfg, dict) else {}
    reg = registry if registry is not None else load_lora_registry()
    rentry = reg.get(_persona_key(persona)) if isinstance(reg, dict) else None
    rentry = rentry if isinstance(rentry, dict) else {}

    def _pick(pkey: str, rkey: str, ckey: str, default: Any = "") -> Any:
        if isinstance(persona, dict) and persona.get(pkey) is not None:
            return persona.get(pkey)
        if rentry.get(rkey) is not None:
            return rentry.get(rkey)
        v = lcfg.get(ckey)
        return v if v is not None else default

    file = str(_pick("lora_file", "file", "file", "") or "").strip()
    trigger = str(_pick("lora_trigger", "trigger", "trigger", "") or "").strip()
    wraw = _pick("lora_weight", "weight", "weight", 1.0)
    try:
        weight = float(wraw if wraw not in (None, "") else 1.0)
    except Exception:
        weight = 1.0
    return {"file": file, "weight": weight, "trigger": trigger}


# 尺度分级 clause（喂 FLUX 的**正面**措辞——cfg=1 无负面提示词，靠正面措辞钉死画面）：
#   sfw        —— 完全遮盖、日常向（默认、历史行为不变）；
#   suggestive —— 性感但**不露骨**：允许身材展现/暧昧姿态/低胸泳装内衣，
#                 但硬保留「不露点、不色情」底线（真正的露骨拦截由 image_gate 兜底）。
# 刻意**不提供 explicit 档**：露骨内容越过平台/法律线，产品层不开这个口子。
_RATING_CLAUSES = {
    "sfw": "fully clothed, tasteful, safe-for-work, no nudity",
    "suggestive": (
        "alluring, flirtatious and sensual, tasteful glamour, "
        "figure-flattering or form-fitting outfit, confident seductive expression, "
        "tasteful and not pornographic, no full nudity, no exposed intimate parts"
    ),
}


def normalize_content_rating(content_rating: str = "", *, sfw: bool = True) -> str:
    """归一化尺度档位（纯函数）：``sfw`` | ``suggestive`` | ``none``。

    ``content_rating`` 显式给了则优先（未知值 → 保守回落 sfw）；为空时由旧参数 ``sfw``
    推：``sfw=True`` → ``"sfw"``，``sfw=False`` → ``"none"``（保持历史「无约束」边界）。
    ``explicit`` 被刻意拦回 ``suggestive``——产品层不出露骨。
    """
    r = str(content_rating or "").strip().lower()
    if not r:
        return "sfw" if sfw else "none"
    if r in ("suggestive", "sensual", "spicy"):
        return "suggestive"
    if r in ("explicit", "nsfw", "porn"):
        return "suggestive"  # 露骨诉求一律降级到不露骨（硬护栏，不给 explicit 口子）
    return "sfw"


def build_selfie_prompt(
    persona: Any,
    *,
    scene_hint: str = "",
    style: str = "",
    default_appearance: str = "",
    sfw: bool = True,
    content_rating: str = "",
    variety_salt: Optional[int] = None,
    lora_trigger: str = "",
) -> str:
    """构造形象照生成提示词（纯函数）。强制安全约束 + 强制人类单人主体。

    优先级：persona 真实外貌（appearance/visual/…）→ ``default_appearance``（config 可配）
    → 结构化字段推断（gender/age，见 ``_persona_smart_base``）→ 中性兜底。
    注意：任何分支都**不再**把人设原名（中文/非拉丁）写进 prompt——那对生图模型是噪声，
    还可能诱发画面里渲染文字。

    ``content_rating``（尺度分级，默认 ``sfw`` = 历史行为不变）：``suggestive`` 出「性感
    不露骨」照（擦边变现，仍硬保留不露点措辞 + image_gate 兜底拦露骨）；见
    ``normalize_content_rating``。空值时由旧参数 ``sfw`` 推（向后兼容）。

    ``variety_salt``（治「千篇一律」）：None=旧行为（恒「looking at the camera」，
    向后兼容、测试口径不变）；给了整数 → 经 ``selfie_variety`` 注入可轮换的取景/头部
    姿态/视线/表情/写实质感，让「同一个人」呈现不同自然瞬间。单人锚「solo, one person」
    始终保留（防跑偏）。

    ``lora_trigger``（角色 LoRA 部署）：非空则作**首个** token 前置（LoRA 靠训练时的
    稀有触发词唤起身份，须领衔）；空=不加（无 LoRA 时零影响，向后兼容）。
    """
    base = (
        _persona_visual(persona)
        or str(default_appearance or "").strip()
        or _persona_smart_base(persona)
        or "a warm, friendly young woman, gentle expression"
    )
    v = selfie_variety(variety_salt) if variety_salt is not None else None
    parts = []
    trig = str(lora_trigger or "").strip()
    if trig:
        parts.append(trig)  # LoRA 触发词领衔（激活训练的身份）
    parts.append(f"Portrait selfie photo of {base}")
    # 反跑偏硬约束：单人、真人（FLUX cfg=1 无负面提示词，只能靠正面措辞钉死主体）。
    # 视线/景别/姿态/表情：有 variety_salt 时随池轮换（自然多样），否则恒「看镜头」。
    if v is not None:
        parts.append("solo, one person")
        parts.append(f"{v['framing']}, {v['head']}, {v['gaze']}, {v['expr']}")
    else:
        parts.append("solo, one person, looking at the camera")
    sc = str(scene_hint or "").strip()
    if sc:
        parts.append(sc)
    st = str(style or "").strip() or "natural lighting, candid, photorealistic, high quality"
    parts.append(st)
    if v is not None:
        parts.append(v["realism"])
    rating = normalize_content_rating(content_rating, sfw=sfw)
    if rating == "suggestive":
        # 成年锚：suggestive 档钉「成年」，与 image_gate 未成年硬红线双保险。
        # （sfw 档不加，保持历史 prompt 逐字不变。）
        parts.append("adult, mature")
    clause = _RATING_CLAUSES.get(rating)
    if clause:
        parts.append(clause)
    return ", ".join(p for p in parts if p)


def stable_selfie_seed(key: str, salt: int = 0) -> int:
    """按 persona 键派生确定性种子 ∈ [0, 2^31)：同一人设的自拍跨请求共享噪声基线，
    配合固定 appearance 让"同一个人"的观感更稳（彻底锁脸靠相册复用/img2img，见 autosend）。
    空键返回 -1（=随机种子）。

    ``salt``（治「构图千篇一律」）：默认 0=旧行为（同人设恒定底噪，向后兼容）；
    非 0 时把 salt 掺进派生 → 同人设不同 salt 得到不同底噪（构图/姿态随之变化），
    身份一致性交由 PuLID/角色 LoRA 保障。调用方传每次发送变化的 salt 即每张构图不同。
    """
    k = str(key or "").strip()
    if not k:
        return -1
    import zlib

    try:
        s = int(salt or 0)
    except Exception:
        s = 0
    payload = k if s == 0 else f"{k}#{s}"
    return zlib.crc32(payload.encode("utf-8")) % (2 ** 31)


def scene_pool(persona: Any, fallback_scenes: Any = None) -> list:
    """场景池解析（纯函数）：persona ``selfie_scenes`` 优先，回落 config 列表；无则空。"""
    if isinstance(persona, dict):
        raw = persona.get("selfie_scenes")
        if isinstance(raw, (list, tuple)) and raw:
            pool = [str(x).strip() for x in raw if str(x).strip()]
            if pool:
                return pool
    if isinstance(fallback_scenes, (list, tuple)):
        return [str(x).strip() for x in fallback_scenes if str(x).strip()]
    return []


# ── 场景-时段硬冲突过滤（2026-07-14 Phase19 时间一致性）────────────────────────
# 背景：bucket 只是轮换索引偏移、不做语义匹配 → 凌晨 3 点可能取到
# "campus walkway, afternoon light"（深夜发白天图，直接穿帮）。
# 词表刻意保守：只剔**硬冲突**（深夜 vs 白天词），模糊光线词（lamp light/
# window light 白天黑夜都成立）不剔——宁可放过，不误杀人设场景池。
_SCENE_DAY_WORDS = ("morning", "sunrise", "dawn", "noon", "midday", "afternoon",
                    "daytime", "daylight", "sunny day")
_SCENE_EVENING_WORDS = ("evening", "sunset", "dusk", "golden hour")
_SCENE_NIGHT_WORDS = ("night", "midnight", "late night")


def scene_conflicts_with_hour(scene: str, hour: int) -> bool:
    """场景短语的时间词与当前小时是否**硬冲突**（纯函数）。

    白天(6-17)剔夜景；傍晚(17-22)剔清晨/正午；深夜(22-6)剔全部白天+黄昏词。
    无时间词恒 False（中性场景任何时段可用）。
    """
    s = str(scene or "").lower()
    if not s:
        return False
    h = int(hour)
    if 6 <= h < 17:
        return any(w in s for w in _SCENE_NIGHT_WORDS)
    if 17 <= h < 22:
        return any(w in s for w in ("morning", "sunrise", "dawn", "noon", "midday"))
    return any(w in s for w in _SCENE_DAY_WORDS + _SCENE_EVENING_WORDS)


def pick_scene_hint(
    persona: Any,
    *,
    default_scene: str = "",
    fallback_scenes: Any = None,
    now: Any = None,
    salt: int = 0,
) -> str:
    """场景轮换（纯函数）：按「日期 + 时段 + salt」从场景池确定性取一条。

    池优先级：persona ``selfie_scenes``（贴人设的场景，如学生=宿舍/便利店、
    金融顾问=办公室/高尔夫）→ config ``scene_rotation`` 列表 → ``default_scene``。
    同一天同一时段取值稳定（同 seed 下重复请求不出两张不同图）；``salt``（如相册
    已有张数）用于自动扩容时错开场景。时段：晨/午/晚/夜 4 档。

    2026-07-14（Phase19）：先按当前时段剔除**硬冲突**场景（凌晨不取
    "afternoon light"——聊天注入/生图都从这取，深夜发白天图直接穿帮）再轮换；
    全池冲突则回退原池（有场景总比没场景强）。
    """
    pool = scene_pool(persona, fallback_scenes)
    if not pool:
        return str(default_scene or "").strip()
    import datetime as _dt

    t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    h = t.hour
    fitting = [s for s in pool if not scene_conflicts_with_hour(s, h)]
    if fitting:
        pool = fitting
    bucket = 0 if 6 <= h < 11 else 1 if 11 <= h < 17 else 2 if 17 <= h < 22 else 3
    idx = (t.timetuple().tm_yday * 4 + bucket + int(salt)) % len(pool)
    return pool[idx]


_TIME_PHRASE_BY_HOUR = (
    ((5, 8), "early morning soft light"),
    ((8, 11), "morning light"),
    ((11, 14), "midday light"),
    ((14, 17), "afternoon light"),
    ((17, 20), "evening, golden hour light"),
    ((20, 23), "night, warm indoor light"),
)
_LATE_NIGHT_PHRASE = "late night, dim cozy indoor light"
_ANY_TIME_WORDS = (_SCENE_DAY_WORDS + _SCENE_EVENING_WORDS + _SCENE_NIGHT_WORDS)


def ensure_time_of_day(scene: str, now: Any = None) -> str:
    """给**没有时间词**的场景短语补当前时段光线氛围（纯函数，Phase19）。

    用途：LLM 发图指令（photo_directive）的场景直通生图 prompt——LLM 通常会带
    时间（协议要求），漏了就按服务器当前时间兜底，保证凌晨要图不出正午烈日照。
    已有任何时间词 → 原样返回（尊重 LLM 的对话内理解，它可能在描述"上次白天拍的"）。
    空场景返回空（交上游轮换池路径，那边已有时段过滤）。
    """
    sc = str(scene or "").strip()
    if not sc:
        return sc
    low = sc.lower()
    if any(w in low for w in _ANY_TIME_WORDS):
        return sc
    import datetime as _dt
    t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    h = t.hour
    phrase = _LATE_NIGHT_PHRASE
    for (lo, hi), p in _TIME_PHRASE_BY_HOUR:
        if lo <= h < hi:
            phrase = p
            break
    return sc + ", " + phrase


def build_scene_choice_instruction(
    directive: str, facts: Any, scenes: list,
) -> str:
    """「场景反选」LLM 指令（纯函数，Phase18）：按主动话题从场景池选最贴合的一条。

    例：回访"备考 N1" → 选"宿舍书桌/图书馆"而非"夜市"。只输出编号；0=没有明显贴合
    （调用方回落时段轮换）。解析见 ``parse_scene_choice``。
    """
    lines = [f"{i + 1}. {s}" for i, s in enumerate(scenes)]
    fs = [str(f).strip() for f in (facts or []) if str(f).strip()][:3]
    fact_block = ("；相关背景：" + "；".join(fs)) if fs else ""
    return (
        "你在为一条聊天消息选配图场景。消息话题：「"
        + str(directive or "").strip()[:160] + "」" + fact_block + "\n"
        "候选场景（英文描述）：\n" + "\n".join(lines) + "\n"
        "选出与话题最贴合的场景编号；如果没有明显贴合的，回答 0。"
        "只输出一个数字，不要任何其它内容。"
    )


def parse_scene_choice(reply: Any, n: int) -> int:
    """解析场景反选回答 → 编号 ∈ [0, n]；解析不出/越界返回 -1（调用方回落轮换）。"""
    import re

    m = re.search(r"-?\d+", str(reply or ""))
    if not m:
        return -1
    try:
        v = int(m.group(0))
    except Exception:
        return -1
    return v if 0 <= v <= int(n) else -1


def resolve_current_scene(
    persona: Any,
    scfg: Dict[str, Any],
    *,
    now: Any = None,
    salt: int = 0,
) -> str:
    """「AI 此刻在哪/在干嘛」的**单一事实源**（纯函数，Phase18 场景状态化）。

    背景（图文打脸事故面）：聊天文本 LLM 自由发挥（"我在上班"），生图链独立按
    日期轮换场景（出"海边落日"）——两者都是果，没有共同的因。本函数把
    ``pick_scene_hint`` 的确定性轮换（同日同时段恒定）升格为会话级「当前场景状态」：
    聊天 prompt（``scene_chat_note``）与生图 prompt（Stage A / autosend / proactive）
    都从这里取，图文天然同源，零 LLM 调用、零提取误判。

    取值口径与既有生图链完全一致（persona ``selfie_scenes`` → config
    ``scene_rotation`` → ``scene_hint``），保证收口不改变已有出图行为。
    """
    return pick_scene_hint(
        persona,
        default_scene=str((scfg or {}).get("scene_hint") or ""),
        fallback_scenes=(scfg or {}).get("scene_rotation"),
        now=now,
        salt=salt,
    )


# 行程线四时段（代表小时须与 pick_scene_hint 的 bucket 划分一一对应：
# 6-11 上午 / 11-17 下午 / 17-22 傍晚 / 22-6 深夜）——同一确定性函数按各桶取值，
# 「当前真实时刻」与「行程线当前桶」必然同场景（同 bucket+yday+salt）。
_ITINERARY_BUCKETS = ((8, "上午"), (13, "下午"), (19, "傍晚"), (23, "深夜"))


def _bucket_label(hour: int) -> str:
    h = int(hour)
    if 6 <= h < 11:
        return "上午"
    if 11 <= h < 17:
        return "下午"
    if 17 <= h < 22:
        return "傍晚"
    return "深夜"


def build_day_itinerary(
    persona: Any, scfg: Dict[str, Any], *, now: Any = None,
) -> list:
    """「今天的动线」（纯函数，Phase20 时间叙事）：把场景状态从「点」升级为「线」。

    同一个确定性轮换（``resolve_current_scene``）按今天四个时段各取一条 →
    ``[(时段名, 场景), ...]``——聊天注入后 LLM 可自然引用「早上去过哪/晚点打算
    干嘛」（过去/将来时态），跨时段叙事连贯；每桶各自经 Phase19 时段冲突过滤
    （深夜桶不会排到 afternoon 场景）。零 LLM、零存储：明天动线自动换（yday 变）。
    场景池空 → []（调用方不渲染行程线）。
    """
    import datetime as _dt
    t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    out = []
    for hour, label in _ITINERARY_BUCKETS:
        try:
            rep = t.replace(hour=hour, minute=0, second=0, microsecond=0)
            sc = resolve_current_scene(persona, scfg, now=rep)
        except Exception:
            sc = ""
        if sc:
            out.append((label, sc))
    return out


def scene_chat_note(
    scene: str, itinerary: Any = None, *, now: Any = None,
) -> str:
    """把场景短语渲染成聊天 prompt 的「状态设定」块（纯函数）。

    措辞要点：内部设定而非播报指令——只在对方问起/话题相关时自然引用，
    防 LLM 每条都汇报位置（那比不一致更机器人）。场景池是英文短语（生图用），
    LLM 需换成口语转述而非原样输出。
    ``itinerary``（Phase20 可选）＝``build_day_itinerary`` 输出：注入「今天动线」
    一行，LLM 获得跨时段叙事（早上做过什么/晚点打算什么），当前时段以（现在）标注。
    """
    sc = str(scene or "").strip()
    if not sc:
        return ""
    lines = [
        "【你此刻的状态（内部设定）】你现在的场景：" + sc + "。",
    ]
    rows = [r for r in (itinerary or [])
            if isinstance(r, (list, tuple)) and len(r) == 2 and str(r[1]).strip()]
    if len(rows) >= 2:
        import datetime as _dt
        t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
        cur = _bucket_label(t.hour)
        seq = "→".join(
            f"{label}{'(现在)' if label == cur else ''}:{str(s).strip()}"
            for label, s in rows)
        lines.append(
            "你今天的动线：" + seq + "。可自然提到今天早些时候做过的事"
            "（过去时）或晚点的打算（将来时），前后保持一致。")
    lines.append(
        "仅当对方问你在哪/在干嘛、或话题自然相关时，用口语顺带提到（翻译成对话语言，"
        "不要输出英文原文、不要每条都汇报位置、不要与它矛盾——比如场景在家就别说自己在公司加班）。")
    return "\n".join(lines)


# 用户显式点名的拍摄场景（"发张你在海边的照片"）→ 生图 directive.scene 覆盖轮换。
# 保守词表（宁缺勿滥）：只收高置信、名词性、可安全出图的场景；衣着/姿态类不收（易滑向 NSFW 请求，
# 交由 SFW 硬约束与正常轮换处理）。value 为英文生图短语（与 scene_rotation 池同格式）。
_REQUESTED_SCENE_MAP: tuple = (
    (("海边", "海邊", "沙滩", "沙灘", "beach", "seaside"),
     "at the beach, sea in the background"),
    (("咖啡厅", "咖啡廳", "咖啡店", "cafe", "coffee shop"),
     "in a cozy cafe, holding a coffee cup"),
    (("办公室", "辦公室", "上班", "公司", "office"),
     "at the office desk, workday casual"),
    (("健身房", "健身", "gym", "workout"),
     "at the gym in sporty outfit, post-workout glow"),
    (("厨房", "廚房", "做饭", "做菜", "煮饭", "kitchen", "cooking"),
     "in the kitchen cooking, apron on"),
    (("公园", "公園", "户外", "戶外", "park", "outdoors"),
     "outdoors in a park, natural daylight"),
    (("家里", "家裡", "在家", "沙发", "沙發", "at home", "on the couch"),
     "at home on the couch, cozy and relaxed"),
    (("卧室", "臥室", "床上", "bedroom"),
     "in the bedroom, soft warm light, fully clothed casual"),
    (("图书馆", "圖書館", "书店", "書店", "library", "bookstore"),
     "in a quiet library with bookshelves"),
    (("街上", "街头", "街頭", "逛街", "street", "shopping"),
     "on a city street, casual street style"),
    (("雨", "下雨", "rain", "rainy"),
     "by the window on a rainy day"),
    (("夜景", "晚上外面", "night view"),
     "city night lights in the background"),
)


def extract_requested_scene(text: str) -> str:
    """从客户要图消息里提取**显式点名的场景**（纯函数；没点名返回空串走正常轮换）。

    只在文本本身是要图请求时才有意义（调用方先过 detect_selfie_request /
    offer-accept 桥）；这里只做词表映射，刻意不用 LLM（要图路径延迟敏感）。
    """
    t = str(text or "").strip().lower()
    if not t or len(t) > 200:
        return ""
    for keys, scene in _REQUESTED_SCENE_MAP:
        for k in keys:
            if k in t:
                return scene
    return ""


# 「跟上次一样的」指涉（要图请求里点名复刻上次场景）→ 调用方从已发媒体日志取上次 scene。
_SAME_SCENE_MARKERS = (
    "跟上次一样", "和上次一样", "同上次", "上次那样", "上次那种", "像上次",
    "跟上次一樣", "和上次一樣", "上次那樣", "上次那種",
    "same as last", "like last time", "like the last one",
)


def wants_same_scene(text: str) -> bool:
    """客户是否在要「跟上次一样场景」的照片（纯函数；场景本身由调用方查日志）。"""
    t = str(text or "").strip().lower()
    if not t or len(t) > 200:
        return False
    return any(m in t for m in _SAME_SCENE_MARKERS)


_STAGE_TEXTS: dict = {
    # Stage 短路直发文案（绕过 LLM 与出站翻译）——硬编码单语会对外语客户穿帮
    # （英文会话突然蹦中文）。zh/en 双语按会话语言取；其余语种暂用 en（比 zh 更
    # 通用），后续可接翻译引擎。{name} = 人设名。
    "too_soon": {
        "zh": "哎呀，我们才刚开始熟悉呢，等再多聊聊、更亲近一点，{name}就给你看我的样子好不好～",
        "en": "hehe, we just started getting to know each other~ chat with me a bit more and I'll show you what I look like 😊",
    },
    "capped": {
        "zh": "{name}今天已经拍了好多照片啦，有点累咯～明天再给你拍新的好不好？😊",
        "en": "I've taken so many photos today, a little tired now~ I'll take a new one for you tomorrow, okay? 😊",
    },
    "no_photo": {
        "zh": "{name}现在不太方便拍照呢，不过我一直在这儿陪你～想我了的话，多跟我说说话好不好？",
        "en": "I can't really take a photo right now~ but I'm right here with you. Talk to me a bit more, okay? 😊",
    },
    "caption": {
        "zh": "这是刚拍的，给你看～ 喜欢{name}吗？😊",
        "en": "just took this for you~ do you like it? 😊",
    },
    "caption_object": {
        "zh": "拍好啦，给你看～😊",
        "en": "here you go, just took it~ 😊",
    },
    "promise_fail": {
        # 异步兑现失败的补偿（承诺文本已发出、图没出来）：像真人一样找个台阶，
        # 不否认能力、给出「改天补」的软承诺（远期承诺不在守卫打击面）。
        "zh": "呜呜刚拍好想发给你，手机突然抽风传不上去…改天一定补给你，别嫌弃我嘛😢",
        "en": "ugh I took one but my phone is acting up and it won't upload... I'll make it up to you another day, promise 😢",
    },
    "upsell_lead": {
        "zh": "我的照片是只给最亲近的人看的小秘密哦～",
        "en": "my photos are a little secret I only share with someone really close~ ",
    },
    "upsell_fallback": {
        "zh": "解锁「专属相册」就能看到{name}啦～",
        "en": "unlock my private album and you'll get to see me~ 😊",
    },
}


def selfie_stage_text(key: str, lang: str = "", *, persona_name: str = "") -> str:
    """Stage 短路搪塞/兜底/配文文案（纯函数）：按会话语言取 zh/en 模板。

    ``lang`` 为空或 zh* → 中文（产品主语言）；其余一律英文。运营在 config 里
    配置的 ``caption``/``scene_hint`` 等自定义值优先于本函数（调用方先查 config）。
    """
    entry = _STAGE_TEXTS.get(str(key or ""), {})
    if not entry:
        return ""
    lg = str(lang or "").strip().lower()
    use_zh = (not lg) or lg.startswith("zh")
    tpl = entry["zh"] if use_zh else entry["en"]
    name = str(persona_name or "").strip()
    if use_zh:
        name = name or "我"
    elif not name or name == "我":
        name = "me"  # 中文占位名不能漏进英文模板
    try:
        return tpl.format(name=name)
    except Exception:
        return tpl


def build_photo_caption_instruction(
    peer_text: str,
    *,
    kind: str = "selfie",
    subject: str = "",
    persona_name: str = "",
    scene: str = "",
) -> str:
    """构造「照片配文」LLM 指令（纯函数）——文图协同的关键一环。

    背景：autosend 的草稿文本是**在不知道会发图**的情况下生成的，常写出
    "等我现在去拍一张？等我一下～"，而图早已附上 → 文图自相矛盾（实录事故）。
    本指令让 LLM 在**明知照片已发出**的前提下补一句贴上下文的配文。
    ``scene``（Phase18）＝这张照片实际的拍摄场景（英文短语，与生图 prompt 同源）：
    给了就让配文自然贴场景（"便利店夜班好无聊～"），图文叙事一体。
    解析/调用在 autosend 侧（保持本模块纯净可测）。
    """
    who = f"「{persona_name}」" if persona_name else "一个真人"
    if kind == "object" and subject:
        desc = f"一张「{subject}」的照片"
    elif kind == "video":
        desc = "一段你的小视频"
    else:
        desc = "一张你刚拍的自拍照"
    pt = str(peer_text or "").strip()[:120]
    sc = str(scene or "").strip()[:120]
    scene_line = (
        f"照片的拍摄场景是：{sc}（配文可自然带到你正在做的事，用对话语言口语化转述，"
        "不要输出英文原文）。\n" if sc else "")
    return (
        f"你在聊天软件里扮演{who}（真人口吻）。你刚刚把{desc}发给了对方。\n"
        f"对方刚才的消息：「{pt}」\n"
        + scene_line +
        "写一条随照片一起发送的配文，要求：\n"
        "- 使用与对方消息相同的语言\n"
        "- 口语化、自然、不超过 30 字，最多 1 个 emoji\n"
        "- 照片已经发出去了：禁止写「等我去拍」「我发不了照片」这类否认或拖延的话\n"
        "- 不要复述对方的话，不要加引号\n"
        "只输出配文正文。"
    )


def decide_selfie(
    *,
    entitlement: Optional[Dict[str, Any]],
    gate_enabled: bool,
    free_used: int,
    free_daily: int,
    bond_level: int = 0,
    min_bond_level: int = 0,
) -> Dict[str, Any]:
    """形象照准入决策（纯函数）。返回 ``{action, feature, used_free}``。

    - ``too_soon``：关系等级不足（避免一上来就要照片的轻浮感）。
    - ``allow``：已拥有相册 / gate 关（不计费）→ 不限；否则免费额度内 → ``used_free=True``。
    - ``locked``：gate 开 + 未拥有 + 免费额度用尽 → 走 exclusive_album 付费引导。
    """
    if int(bond_level) < int(min_bond_level):
        return {"action": "too_soon", "feature": SELFIE_FEATURE, "used_free": False}
    if feature_allowed(entitlement, SELFIE_FEATURE, gate_enabled=bool(gate_enabled)):
        return {"action": "allow", "feature": SELFIE_FEATURE, "used_free": False}
    if int(free_used) < max(0, int(free_daily)):
        return {"action": "allow", "feature": SELFIE_FEATURE, "used_free": True}
    return {"action": "locked", "feature": SELFIE_FEATURE, "used_free": False}


@dataclass
class SelfieResult:
    ok: bool = False
    image_path: str = ""
    prompt: str = ""
    provider: str = ""
    latency_ms: int = 0
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# album 后端可挑选的图片扩展名。
_ALBUM_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class SelfieProvider:
    """形象照生成 provider（软失败骨架，镜像 TTSPipeline）。

    Config（``companion.selfie.provider``）：
        enabled: false
        backend: disabled | openai | command
        model/size/api_key/base_url：openai images 用（model=gpt-image-1 默认；亦支持 dall-e-3）
        quality: 可选（gpt-image-1：low|medium|high|auto）；request_timeout_sec: 单请求超时(默认 60)
        command_args / command_template：本地推理（ComfyUI/SD 脚本），占位 {prompt}/{out}
        out_dir: tmp_selfies
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.backend = str(cfg.get("backend", "disabled")).strip().lower()
        self.model = str(cfg.get("model") or "gpt-image-1").strip()
        self.size = str(cfg.get("size") or "1024x1024").strip()
        self.quality = str(cfg.get("quality") or "").strip()
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.request_timeout_sec = float(cfg.get("request_timeout_sec", 60) or 60)
        self.out_dir = Path(str(cfg.get("out_dir") or "tmp_selfies"))
        # backend=album：从预制相册随机挑图（不出图、零 API 费、同一张脸最一致）。
        self.album_dir = Path(str(cfg.get("album_dir") or "config/persona_albums"))
        self.command_args = cfg.get("command_args")
        # 无脸/轻量图（物体图、无 face_ref 自拍）可走独立命令（如指向另一台 ComfyUI），
        # 把重的锁脸自拍留给主卡、分流 GPU。缺省=沿用 command_args（零行为变更）。
        self.command_args_noface = cfg.get("command_args_noface")
        self.command_template = str(cfg.get("command_template") or "").strip()
        self.command_timeout_sec = float(cfg.get("command_timeout_sec", 180) or 180)

    def stats(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "backend": self.backend,
                "model": self.model, "out_dir": str(self.out_dir),
                "album_dir": str(self.album_dir)}

    async def generate(
        self, prompt: str, *, timeout_sec: Optional[float] = None,
        album_key: str = "", avoid_path: str = "", base_image: str = "",
        seed: int = -1, lora: str = "", lora_weight: float = 1.0,
    ) -> SelfieResult:
        """出图。``base_image`` 非空且存在 → img2img（openai images.edit / command ``{base}``），
        用于锁住人设一致性；album 后端忽略 prompt/base（只挑现成图）。
        ``seed`` ≥0 时透传给 command 后端 ``{seed}`` 占位（人设自拍固定种子稳外观）；
        -1=随机（openai 后端无种子概念，忽略）。
        ``lora``/``lora_weight``（角色 LoRA 部署）：透传给 command 后端 ``{lora}``/
        ``{lora_weight}`` 占位（per-persona 各自的 LoRA 文件）；openai/album 忽略。"""
        rv = SelfieResult(prompt=str(prompt or ""), provider=self.backend)
        if not self.enabled or self.backend in ("", "disabled"):
            rv.error = "provider_disabled"
            return rv
        # album 后端：不出图，从预制相册挑一张已有照片（无需 prompt）。
        if self.backend == "album":
            return self._pick_from_album(album_key=album_key, avoid_path=avoid_path)
        if not rv.prompt.strip():
            rv.error = "empty_prompt"
            return rv
        # 外层 wait_for 是兜底：须严格大于 client/命令各自的请求超时，否则会在请求合法运行中途
        # 误砍，掩盖掉底层（client.timeout / command_timeout）的精确错误。取请求超时 + 15s 余量。
        inner = (self.command_timeout_sec if self.backend == "command"
                 else self.request_timeout_sec)
        eff_timeout = float(timeout_sec) if timeout_sec else float(inner) + 15.0
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out = self.out_dir / f"selfie-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png"
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self._generate_sync, rv.prompt, out, base_image, seed,
                    lora, lora_weight),
                timeout=eff_timeout,
            )
            if out.exists() and out.stat().st_size > 0:
                rv.ok = True
                rv.image_path = str(out)
                rv.extra["bytes"] = out.stat().st_size
                rv.extra["img2img"] = bool(base_image)
            else:
                rv.error = "empty_image"
        except asyncio.TimeoutError:
            rv.error = f"selfie_timeout({eff_timeout:.0f}s)"
        except Exception as ex:  # noqa: BLE001
            rv.error = f"{type(ex).__name__}: {ex}"
        rv.latency_ms = int((time.monotonic() - t0) * 1000)
        return rv

    def _album_dirs(self, album_key: str = "") -> list:
        """候选相册目录：优先 ``album_dir/<persona_key>``（多人设分册），回落 ``album_dir`` 根目录。

        ``album_key`` 只保留字母/数字（含 CJK）/``-``/``_``——挡掉路径分隔符与 ``..``，防目录穿越。
        """
        dirs: list = []
        key = "".join(c for c in str(album_key or "") if c.isalnum() or c in ("-", "_"))
        if key:
            dirs.append(self.album_dir / key)
        dirs.append(self.album_dir)
        return dirs

    def _list_album(self, album_key: str = "") -> list:
        """列出候选目录里第一个含图片的目录的所有图片路径（排序稳定）；无则空列表。"""
        for d in self._album_dirs(album_key):
            try:
                if d.is_dir():
                    files = sorted(
                        str(p) for p in d.iterdir()
                        if p.is_file() and p.suffix.lower() in _ALBUM_IMAGE_EXT
                    )
                    if files:
                        return files
            except Exception:  # noqa: BLE001
                continue
        return []

    def _pick_from_album(
        self, *, album_key: str = "", avoid_path: str = ""
    ) -> SelfieResult:
        """从预制相册随机挑一张（尽量避开上一张 ``avoid_path``，避免连发同图）。"""
        files = self._list_album(album_key)
        if not files:
            return SelfieResult(provider="album", error="album_empty")
        pool = [f for f in files if f != str(avoid_path or "")] or files
        pick = random.choice(pool)
        return SelfieResult(ok=True, image_path=pick, provider="album",
                            extra={"album_size": len(files)})

    def reference_image(self, album_key: str = "") -> str:
        """挑一张相册图当"基础图/锁脸参考"（openai/command 后端 img2img 用）；无相册回空串。

        让 album_dir 一物两用：album 后端直接发它，openai/command 后端拿它当 img2img 基础图，
        使生成的人设照片保持同一张脸。

        优先选名为 ``face_ref.*`` 的"官方基准脸"（PuLID 锁脸约定，运营手工定妆的那张，
        不随相册增删漂移）；没有才回落排序第一张。
        """
        files = self._list_album(album_key)
        for f in files:
            if Path(f).stem.lower() == "face_ref":
                return f
        return files[0] if files else ""

    def _generate_sync(
        self, prompt: str, out: Path, base_image: str = "", seed: int = -1,
        lora: str = "", lora_weight: float = 1.0,
    ) -> None:
        if self.backend == "openai":
            self._generate_openai(prompt, out, base_image)
            return
        if self.backend == "command":
            self._generate_command(prompt, out, base_image, seed, lora, lora_weight)
            return
        raise RuntimeError(f"unknown backend {self.backend}")

    def _generate_openai(self, prompt: str, out: Path, base_image: str = "") -> None:
        client = self._make_openai_client()
        if base_image and Path(base_image).is_file():
            out.write_bytes(self._openai_edit_bytes(client, prompt, base_image))
        else:
            out.write_bytes(self._openai_generate_bytes(client, prompt))

    def _make_openai_client(self) -> Any:
        """构造 OpenAI 客户端（独立测试缝：测试可 monkeypatch 本方法注入假 client）。"""
        from openai import OpenAI  # type: ignore
        if not self.api_key:
            raise RuntimeError("missing api_key for openai images")
        kwargs: Dict[str, Any] = {"api_key": self.api_key,
                                  "timeout": self.request_timeout_sec}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def _openai_generate_bytes(self, client: Any, prompt: str) -> bytes:
        """调 images.generate 并取回 PNG 字节。model 感知 + b64/url 双回退。

        - ``gpt-image-1``：恒返回 b64（且**不接受** response_format 参数，传了会报错）。
        - ``dall-e-2/3``：默认返回 url；显式要 ``response_format=b64_json`` 才回 b64。
        - 兜底：拿不到 b64 但有 url → 下载 url（兼容自建/代理 images 网关行为差异）。
        """
        req: Dict[str, Any] = {"model": self.model, "prompt": prompt,
                               "size": self.size, "n": 1}
        if self.quality:
            req["quality"] = self.quality
        if self.model.startswith("dall-e"):
            req["response_format"] = "b64_json"
        return self._resp_to_bytes(client.images.generate(**req))

    def _openai_edit_bytes(self, client: Any, prompt: str, base_image: str) -> bytes:
        """基础图 img2img：调 images.edit（gpt-image-1 / dall-e-2 编辑接口），锁住人设一致性。

        传入基础图 + prompt，返回改写后的图；b64/url 双回退同 generate（dall-e-2 需 b64_json）。
        """
        req: Dict[str, Any] = {"model": self.model, "prompt": prompt,
                               "size": self.size, "n": 1}
        if self.model.startswith("dall-e"):
            req["response_format"] = "b64_json"
        with open(base_image, "rb") as fh:
            req["image"] = fh
            resp = client.images.edit(**req)
        return self._resp_to_bytes(resp)

    def _resp_to_bytes(self, resp: Any) -> bytes:
        """从 images 响应（generate/edit 通用）取 PNG 字节：优先 b64_json，回退下载 url。"""
        import base64 as _b64

        data = getattr(resp, "data", None) or []
        if not data:
            raise RuntimeError("openai images: empty response data")
        item = data[0]
        b64 = getattr(item, "b64_json", None) if not isinstance(item, dict) \
            else item.get("b64_json")
        if b64:
            return _b64.b64decode(b64)
        url = getattr(item, "url", None) if not isinstance(item, dict) \
            else item.get("url")
        if url:
            return self._download_image(str(url))
        raise RuntimeError("openai images: no b64_json/url in response")

    def _download_image(self, url: str) -> bytes:
        """下载远端图片（stdlib，不引依赖）；受 request_timeout_sec 约束。"""
        import urllib.request

        with urllib.request.urlopen(url, timeout=self.request_timeout_sec) as r:
            data = r.read()
        if not data:
            raise RuntimeError("openai images: empty download")
        return data

    def _generate_command(
        self, prompt: str, out: Path, base_image: str = "", seed: int = -1,
        lora: str = "", lora_weight: float = 1.0,
    ) -> None:
        raw_args = self.command_args
        # 无脸/轻量图（无 base_image）+ 配了 command_args_noface（list）→ 走它分流 GPU
        # （如物体图/无锁脸自拍指向另一台 ComfyUI）。template 形态不做 noface 路由（保持简单）。
        if not str(base_image or "").strip() and isinstance(self.command_args_noface, list):
            raw_args = self.command_args_noface
        tpl = self.command_template
        if not tpl and not isinstance(raw_args, list):
            raise RuntimeError("selfie command not configured")
        # {base}=基础图路径（img2img，空则为空串——脚本可据此决定 text2img/img2img）；
        # {seed}=生成种子（-1=脚本自选随机；人设自拍传固定值稳外观）；
        # {lora}/{lora_weight}=per-persona 角色 LoRA（空=不挂；命令模板未含占位则忽略）。
        values = {"prompt": prompt, "out": str(out), "base": str(base_image or ""),
                  "seed": str(int(seed)), "lora": str(lora or ""),
                  "lora_weight": str(lora_weight)}
        # text 解码显式 UTF-8 + errors=replace：子进程（comfy_infer）stderr 是 UTF-8，
        # 而 Windows 默认按 GBK 解码 → 中文报错行抛 UnicodeDecodeError 打断线程读取
        # （2026-07-14 实测噪声）。统一 UTF-8 容错解码。
        if isinstance(raw_args, list):
            cmd = [str(x).format(**values) for x in raw_args]
            r = subprocess.run(cmd, shell=False, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=self.command_timeout_sec, env=os.environ.copy())
        else:
            quoted = {k: shlex.quote(v) for k, v in values.items()}
            r = subprocess.run(tpl.format(**quoted), shell=True, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=self.command_timeout_sec,
                               env=os.environ.copy())
        if r.returncode != 0:
            raise RuntimeError(f"selfie_command_failed:{(r.stderr or r.stdout or '')[:300]}")


_selfie_singleton: Optional[SelfieProvider] = None


def get_selfie_provider(cfg: Optional[Dict[str, Any]] = None) -> SelfieProvider:
    global _selfie_singleton
    if _selfie_singleton is None:
        _selfie_singleton = SelfieProvider(cfg or {})
    return _selfie_singleton


def reset_selfie_provider() -> None:
    global _selfie_singleton
    _selfie_singleton = None


__all__ = [
    "SELFIE_FEATURE",
    "detect_selfie_request",
    "build_selfie_prompt",
    "selfie_variety",
    "resolve_variety_salt",
    "resolve_persona_lora",
    "load_lora_registry",
    "stable_selfie_seed",
    "scene_pool",
    "pick_scene_hint",
    "build_scene_choice_instruction",
    "parse_scene_choice",
    "build_photo_caption_instruction",
    "decide_selfie",
    "SelfieResult",
    "SelfieProvider",
    "get_selfie_provider",
    "reset_selfie_provider",
]
