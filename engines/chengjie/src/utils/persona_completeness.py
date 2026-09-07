"""人设完整度评分（纯函数，仅标准库）。

背景：人设工作室只呈现「有没有填」很难量化富人设备货水平；本模块把
prompt 消费端（`persona_manager._format_persona_instructions`）真正会用到的
字段按权重折算成 0-100 分，供 Studio 概览 / API 呈现「这个人设够不够立体」。

约定：
- 键名用点路径（如 ``personality.style``），与 profiles_runtime.yaml 结构对应；
- 所有判空必须健壮——None / 空串 / 空列表 / 类型错（字符串当 dict 等）都不炸，
  错型一律按「未填」计，绝不因脏数据把概览接口打崩；
- ``personality`` 为字符串（自由格式人设）视为 ``personality.style`` 已填
  （与 ``PersonaManager.normalize_profile_shape`` 的收敛语义一致）。
"""

from typing import Any, Dict, List

# 加权清单：prompt 消费端权重越高的字段分越重（background/style/memories 是
# 「真人感」的主料）。总分 = 已得权重 / 总权重 * 100。
FIELD_WEIGHTS: Dict[str, int] = {
    "name": 5,
    "role": 8,
    "personality.style": 10,
    "personality.traits": 8,
    "personality.quirks": 5,
    "personality.temperament": 4,
    "personality.humor": 3,
    "background": 12,
    "names": 4,                          # names 下任一非空子键即算
    "tags": 4,                           # >= 3 个才算
    "context.hobbies": 5,
    "context.specific_memories": 10,     # >=5 条满分，1-4 条算一半
    "context.emotional_triggers": 5,     # 任一子键非空即算
    "speaking.openers": 3,
    "speaking.forbidden_phrases": 3,
    "boundaries.topics_to_avoid": 3,
    "appearance": 4,
    "selfie_scenes": 2,
    "tastes": 4,                         # likes/dislikes/opinions 任一非空
    "voice_profile": 3,                  # backend 或 voice 非空
}

_TOTAL_WEIGHT = sum(FIELD_WEIGHTS.values())

# specific_memories 满分所需条数 / 半分区间下限
_MEMORIES_FULL_AT = 5
_TAGS_MIN = 3

# #238（N-2 C）：相册纳入完整度——有相册却没触发词＝AI 发图只能随机（F35X38 144 张 / K5XHJ2 325 张）。
# 相册不在人设 dict 里，调用方从媒体库取 ``{"total", "with_triggers"}`` 传进来；没相册（total=0）
# 或没传＝不参与评分（权重不进分母），不因「没备货相册」扣分。
ALBUM_KEY = "album.triggers"
ALBUM_WEIGHT = 4
_ALBUM_FULL_RATIO = 0.9


def _album_credit(album: Any) -> float:
    total = int(_dict_get(album, "total") or 0)
    ready = int(_dict_get(album, "with_triggers") or 0)
    if total <= 0:
        return -1.0   # 不参与
    ratio = max(0.0, min(1.0, ready / float(total)))
    if ratio >= _ALBUM_FULL_RATIO:
        return 1.0
    return 0.5 if ratio > 0 else 0.0


def _has_content(v: Any) -> bool:
    """递归判「有实质内容」：空串/空容器/None → False；数字/True → True。"""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, set, frozenset)):
        return any(_has_content(i) for i in v)
    if isinstance(v, dict):
        return any(_has_content(x) for x in v.values())
    # 数字等其余标量：非 None 即算有值
    return True


def _list_count(v: Any) -> int:
    """列表里「有内容」的条目数；非列表按标量降级（有内容算 1）。"""
    if isinstance(v, (list, tuple)):
        return sum(1 for i in v if _has_content(i))
    return 1 if _has_content(v) else 0


def _dict_get(d: Any, key: str) -> Any:
    """健壮取键：宿主不是 dict 一律返回 None（防「字符串当 dict」炸）。"""
    if isinstance(d, dict):
        return d.get(key)
    return None


def _field_credit(persona: Dict[str, Any], key: str) -> float:
    """单字段得分比例 0.0..1.0。"""
    personality = persona.get("personality")

    if key == "personality.style":
        # 自由格式人设：personality 直接是字符串 → 视为 style 已填
        if isinstance(personality, str):
            return 1.0 if personality.strip() else 0.0
        return 1.0 if _has_content(_dict_get(personality, "style")) else 0.0

    if key.startswith("personality."):
        sub = key.split(".", 1)[1]
        return 1.0 if _has_content(_dict_get(personality, sub)) else 0.0

    if key == "tags":
        return 1.0 if _list_count(persona.get("tags")) >= _TAGS_MIN else 0.0

    if key == "context.specific_memories":
        n = _list_count(_dict_get(persona.get("context"), "specific_memories"))
        if n >= _MEMORIES_FULL_AT:
            return 1.0
        if n >= 1:
            return 0.5
        return 0.0

    if key.startswith("context."):
        sub = key.split(".", 1)[1]
        return 1.0 if _has_content(_dict_get(persona.get("context"), sub)) else 0.0

    if key.startswith("speaking."):
        sub = key.split(".", 1)[1]
        return 1.0 if _has_content(_dict_get(persona.get("speaking"), sub)) else 0.0

    if key == "boundaries.topics_to_avoid":
        return 1.0 if _has_content(
            _dict_get(persona.get("boundaries"), "topics_to_avoid")) else 0.0

    if key == "voice_profile":
        vp = persona.get("voice_profile")
        return 1.0 if (
            _has_content(_dict_get(vp, "backend"))
            or _has_content(_dict_get(vp, "voice"))
        ) else 0.0

    # names / tastes / background / appearance / selfie_scenes / name / role：
    # 通用「任一子内容非空」语义（_has_content 对 dict 即「任一子键非空」）。
    return 1.0 if _has_content(persona.get(key)) else 0.0


def persona_completeness(persona: dict, album: Any = None) -> dict:
    """人设完整度评分。

    返回 ``{"score": int 0-100, "filled": [...], "missing": [...]}``：
    - ``filled``：有内容（含半分档）的点路径键；
    - ``missing``：完全没填且权重 >= 4 的缺口（重点提示项，轻量字段不啰嗦）。
    ``album``（可选）：``{"total": N, "with_triggers": M}``——N>0 时 ``album.triggers`` 参与评分
    （≥90% 有触发词满分、部分半分、零分进 missing）；N=0 / 不传＝不参与。
    """
    if not isinstance(persona, dict):
        persona = {}

    earned = 0.0
    filled: List[str] = []
    missing: List[str] = []
    total_weight = _TOTAL_WEIGHT
    for key, weight in FIELD_WEIGHTS.items():
        try:
            credit = _field_credit(persona, key)
        except Exception:
            credit = 0.0  # 单字段脏数据绝不影响整体评分
        if credit > 0:
            earned += weight * credit
            filled.append(key)
        elif weight >= 4:
            missing.append(key)

    try:
        album_credit = _album_credit(album) if isinstance(album, dict) else -1.0
    except Exception:
        album_credit = -1.0
    if album_credit >= 0:
        total_weight += ALBUM_WEIGHT
        if album_credit > 0:
            earned += ALBUM_WEIGHT * album_credit
            filled.append(ALBUM_KEY)
        else:
            missing.append(ALBUM_KEY)

    score = int(round(earned / total_weight * 100)) if total_weight else 0
    return {"score": score, "filled": filled, "missing": missing}
