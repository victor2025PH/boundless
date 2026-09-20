# -*- coding: utf-8 -*-
"""生成侧「AI 指纹」硬禁 + few-shot（P-1 C · #259 #254 · 34585H E/F，2026-09-08）。

O-1 C 的陪伴底稿（``domains/conversion/prompts/system_companion.txt``）已写禁破折号 / 禁客服腔，
但底稿是 system 段、离生成最远；实测 1.0.77 仍 ≈80% 带 —。本模块把**最靠近本轮**的一段硬禁 +
few-shot 挂进 ``persona_reply`` 三条生成路径共用的 extra_hint 消费口（统一引擎 `extra_hint` /
直连 `_topic_switch_hint` / 兜底 prompt），以及开场 ``generate_topic_opener`` 的 directive：

  * 标点：不用破折号（— –）/ 分号 / 项目符号 / 编号 / markdown / 标题；
  * 事实：不引用上下文里没有的「对方说过的话」（you mentioned / 你之前说）；
  * 无历史：首次接触不假装熟悉、不「好久不见」、不回忆；
  * 篇幅：一两句短句；
  * few-shot：按回复语言给 6 组「对方 → 你」示例（en / zh / ja；其余语种只给禁令）。
    有 ``ai.spoken_style`` 桥接包启用的部署，示例交给包（说话指纹 + 轮变尾注），这里只给禁令。

配置 ``inbox.auto_draft.style_hint``：``enabled``（默认开）/ ``few_shot``（默认开）。纯函数、绝不抛。
后处理（起草层 humanize + claim_guard）仍是最终保证；本模块只是让生成侧少产生要被修的东西
（验收：200 样例生成侧破折号 <5%，后处理兜到 0）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")

_BANS_COMMON = (
    "STYLE RULES FOR THIS MESSAGE (hard rules, they override tone advice above):\n"
    "- No em dash or en dash (— –), no semicolon, no bullet points, no numbered list, no markdown, "
    "no headings. Use a comma or start a new sentence instead.\n"
    "- Do not refer to anything the other person supposedly said unless it is literally in the "
    "conversation or the memory notes above. Never write \"you mentioned\", \"you said\", "
    "\"last time you\", 你之前说, 上次你说, 前に言ってた about something that is not there.\n"
    "- One or two short sentences, like a real text message. At most one question.\n"
)
_BAN_NO_HISTORY = (
    "- This is the first exchange with this person. You have no shared past with them: do not "
    "pretend familiarity, do not say you missed them or that it has been a while, do not recall "
    "anything about them.\n"
)

# few-shot：每语 6 组，示例回复零破折号 / 零分号 / 零引用 / ≤2 句
_FEW_SHOT: Dict[str, List[tuple]] = {
    "en": [
        ("just got home, so tired", "same here. dinner first or straight to the couch?"),
        ("it's raining again", "ugh, third day. at least it's a good excuse to stay in."),
        ("do you like hiking?", "yeah, when someone drags me out. you go often?"),
        ("sorry, busy day", "no worries. did it at least go okay?"),
        ("hey", "hey you. how's your evening going?"),
        ("I passed the exam!!", "no way, congrats! how are you celebrating?"),
    ],
    "zh": [
        ("刚到家，累死了", "我也是。先吃饭还是先躺会？"),
        ("又下雨了", "都第三天了。正好有理由不出门。"),
        ("你喜欢徒步吗", "有人拉着就去，你常去？"),
        ("不好意思今天忙", "没事。今天还顺利吗？"),
        ("在吗", "在呢。晚上过得怎么样？"),
        ("我考过了！！", "真的假的，恭喜！打算怎么庆祝？"),
    ],
    "ja": [
        ("やっと帰った、疲れた", "私も。先にご飯？それとも先に横になる？"),
        ("また雨だよ", "三日目だね。出かけない口実にはなる。"),
        ("ハイキング好き？", "誘われたら行くくらい。よく行くの？"),
        ("ごめん今日忙しくて", "大丈夫。今日は順調だった？"),
        ("ねえ", "はい。今夜どんな感じ？"),
        ("試験受かった！！", "うそ、おめでとう！どうお祝いする？"),
    ],
}
_FEW_SHOT_INTRO = {
    "en": "Examples of the register (they → you):",
    "zh": "口吻示例（对方 → 你）：",
    "ja": "トーンの例（相手 → あなた）：",
}


def resolve_cfg(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw: Any = None
    try:
        if config is None:
            from src.compliance.runtime import runtime_config
            config = runtime_config() or {}
        raw = (((config or {}).get("inbox") or {}).get("auto_draft") or {}).get("style_hint")
    except Exception:
        raw = None
    if isinstance(raw, bool):
        raw = {"enabled": raw}
    if not isinstance(raw, dict):
        raw = {}
    return {"enabled": bool(raw.get("enabled", True)), "few_shot": bool(raw.get("few_shot", True))}


def _norm_lang(lang: Any, sample: str = "") -> str:
    s = str(lang or "").strip().lower().replace("_", "-")
    if s and s != "unknown":
        return "zh" if s.startswith("zh") else s.split("-", 1)[0]
    if re.search(r"[\u3040-\u30ff]", sample):
        return "ja"
    if _CJK_RE.search(sample):
        return "zh"
    return "en"


def spoken_style_examples_active(config: Optional[Dict[str, Any]] = None) -> bool:
    """有 spoken_style 桥接包且启用 → few-shot 交给包（说话指纹按人设分流），这里不重复给。"""
    try:
        if config is None:
            from src.compliance.runtime import runtime_config
            config = runtime_config() or {}
        return bool((((config or {}).get("ai") or {}).get("spoken_style") or {}).get("enabled", False))
    except Exception:
        return False


def few_shot_block(lang: str) -> str:
    lg = _norm_lang(lang)
    pairs = _FEW_SHOT.get(lg)
    if not pairs:
        return ""
    lines = [_FEW_SHOT_INTRO.get(lg, _FEW_SHOT_INTRO["en"])]
    for a, b in pairs:
        lines.append(f"- {a} → {b}")
    return "\n".join(lines)


def build_style_hint(lang: str = "", *, has_history: bool = True, config: Optional[Dict[str, Any]] = None,
                     sample_text: str = "") -> str:
    """返回要追加进 extra_hint / directive 的硬禁 + few-shot 段；配置关 → 空串。绝不抛。"""
    try:
        cfg = resolve_cfg(config)
        if not cfg["enabled"]:
            return ""
        parts = [_BANS_COMMON]
        if not has_history:
            parts.append(_BAN_NO_HISTORY)
        if cfg["few_shot"] and not spoken_style_examples_active(config):
            fs = few_shot_block(_norm_lang(lang, sample_text))
            if fs:
                parts.append(fs)
        return "\n".join(p.rstrip("\n") for p in parts if p).strip()
    except Exception:
        logger.debug("[style-hint] build 失败（跳过）", exc_info=True)
        return ""


def history_has_peer_turns(history: Any, *, min_turns: int = 2) -> bool:
    """历史里是否有**对方**说过的话。回复链 ``min_turns=2``（只有本轮这一条入站不算「有共同
    过去」）；开场链无入站 ``min_turns=1``。判不出按 True（宁可少禁）。"""
    try:
        n = 0
        for m in list(history or []):
            if isinstance(m, dict) and str(m.get("role") or m.get("direction") or "") in ("user", "in"):
                if str(m.get("content") or m.get("text") or "").strip():
                    n += 1
        return n >= int(min_turns)
    except Exception:
        return True


__all__ = ["build_style_hint", "few_shot_block", "resolve_cfg", "history_has_peer_turns",
           "spoken_style_examples_active"]
