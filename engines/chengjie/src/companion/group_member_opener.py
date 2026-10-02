"""同群开口的 AI 文案：按这个号的人设和默认工作目标，给一个群里的陌生人写第一句话。

和回复链共用三样东西，不另起一套：
- 人设：``resolve_effective_persona`` 解析到账号级人设，``PersonaManager.build_system_prompt``
  出人设块（和自动回复看到的是同一份人设）。
- 目标：``goals.defaults.get_default`` 读这个号的默认目标模板，取它第 0 相位的意图当「方向」。
  第一条不卖东西——方向只影响语气和切入点。
- 出站守卫：``persona_guard.sanitize`` 剥自曝/客服腔，再过 ``opener_block_reason``
  （链接 / 加联系方式），再查语言是否对得上、和今天别人收到的是否雷同。

LLM 不在 / 生成失败 / 过滤不过 → 回落到有几种变体的模板，不空手。
"""
from __future__ import annotations

import difflib
import logging
import random
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.companion.group_member_outreach import opener_block_reason

logger = logging.getLogger("ai_chat_assistant.group_member_opener")

OPENER_MAX_CHARS_ZH = 60
OPENER_MAX_CHARS_OTHER = 160
SIMILARITY_CEILING = 0.72

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_QUOTE_CHARS = "\"'“”‘’「」『』《》"

_LANG_NAMES = {
    "zh": "中文", "en": "English", "ru": "русский", "es": "español", "pt": "português",
    "id": "Bahasa Indonesia", "vi": "Tiếng Việt", "th": "ไทย", "ja": "日本語", "ko": "한국어",
    "ar": "العربية", "tr": "Türkçe", "de": "Deutsch", "fr": "français",
}

_TEMPLATES_ZH = (
    "{name}，在「{group}」看到你发言，过来打个招呼。",
    "{name}你好，同在「{group}」，看到你说话顺手加个私聊。",
    "{name}，刚在「{group}」看到你那条，想单独聊两句。",
    "{name}，我也在「{group}」，看你挺活跃的，认识一下。",
)
_TEMPLATES_ZH_QUOTE = (
    "{name}，在「{group}」看到你说「{quote}」，想跟你聊聊这个。",
    "{name}，你在「{group}」提到「{quote}」，我也遇到过，私聊说两句？",
)
_TEMPLATES_EN = (
    "Hi {name}, saw your message in {group} and thought I'd say hello.",
    "Hey {name}, we're both in {group}. Figured I'd reach out directly.",
    "{name}, noticed you in {group} and wanted to say hi.",
)
_TEMPLATES_EN_QUOTE = (
    "Hi {name}, you mentioned \"{quote}\" in {group}. Been there too, mind if I ask about it?",
)
# 72h 跟进：轻、给台阶、不追问、不卖
_FOLLOWUP_ZH = (
    "{name}，前两天打过招呼，怕被刷掉了，再冒个泡。不方便聊也没事。",
    "{name}，上次那条可能沉了，补一句：有空再聊，不急。",
    "{name}，还在「{group}」吗？前几天给你发过一句，没看到就当我没说哈。",
)
_FOLLOWUP_EN = (
    "Hey {name}, just bumping my message from the other day - no pressure if now's not a good time.",
    "{name}, my earlier note probably got buried. Whenever you're free, no rush.",
)
# 人设精简块最多这么长：开口 prompt 不需要整份人设（全量格式器 ~3k 字）
PERSONA_BLOCK_MAX = 700

# 开口切入方式 A/B：同一人设、同一目标，只换「从哪儿开口」。按回复率切片择优，
# 留一部分流量探索（ε-greedy），样本不够的方式先多试。
OPENER_VARIANTS = ("echo", "group", "ask")
# 公开 AI 身份的人设（identity.public_ai，如销售人设「小界」）多一种切入：坦白自己是 AI。
AI_INTRO_VARIANT = "ai_intro"
ALL_VARIANTS = OPENER_VARIANTS + (AI_INTRO_VARIANT,)
_VARIANT_HINT = {
    "echo": "切入方式：直接接TA在群里说的那句，像顺着话茬聊下去，不要另起话题。",
    "group": "切入方式：从你们同在的这个群聊起（群里最近的事、你为什么在这个群），不要复述TA的原话。",
    "ask": "切入方式：围绕TA说过的事，轻轻问一个具体的小问题，让TA一句话就能答上来。",
    AI_INTRO_VARIANT: "切入方式：大方说你是个 AI（用人设里的名字和出身），是在这个群里注意到TA才来私聊的；"
                      "用一句轻松或好奇的话收尾，让TA想回。不讲价格、不列功能。",
}
AB_EXPLORE = 0.25
AB_MIN_SAMPLE = 5
AB_LOOKBACK_SEC = 30 * 86400.0
# 先验回复率（经验值）+ 先验权重（相当于几条样本）：样本少时按先验平滑，别被 1/5 vs 2/5 这种噪声带偏。
# 跑出切片结论后在 config `outreach_ab_prior` 里回灌，不用改代码。
AB_PRIOR = {"echo": 0.10, "ask": 0.08, "group": 0.06, AI_INTRO_VARIANT: 0.08}
AB_PRIOR_WEIGHT = 5.0


def ab_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """``companion.group_members`` 里的 A/B 旋钮 → {explore, min_sample, prior}；坏值回默认。"""
    gm = ((cfg or {}).get("companion") or {}).get("group_members") or {}
    out = {"explore": AB_EXPLORE, "min_sample": AB_MIN_SAMPLE, "prior": dict(AB_PRIOR),
           "prior_weight": AB_PRIOR_WEIGHT}
    try:
        out["explore"] = max(0.0, min(float(gm.get("outreach_ab_explore", AB_EXPLORE)), 1.0))
    except (TypeError, ValueError):
        pass
    try:
        out["prior_weight"] = max(0.0, min(float(gm.get("outreach_ab_prior_weight", AB_PRIOR_WEIGHT)), 100.0))
    except (TypeError, ValueError):
        pass
    try:
        out["min_sample"] = max(1, min(int(gm.get("outreach_ab_min_sample", AB_MIN_SAMPLE)), 500))
    except (TypeError, ValueError):
        pass
    prior = gm.get("outreach_ab_prior")
    if isinstance(prior, dict):
        for k, v in prior.items():
            if str(k) in ALL_VARIANTS:
                try:
                    out["prior"][str(k)] = max(0.0, min(float(v), 1.0))
                except (TypeError, ValueError):
                    pass
    return out


def persona_public_ai(persona: Any) -> bool:
    """人设显式公开 AI 身份（``identity.public_ai``）且没配否认 AI / 自称真人。

    只认显式开关：编辑器保存总会写 ``deny_ai=false``，不能拿它当「要坦白」的信号。
    """
    ident = persona.get("identity") if isinstance(persona, dict) else None
    if not isinstance(ident, dict):
        return False
    return (bool(ident.get("public_ai")) and not ident.get("deny_ai")
            and not ident.get("claim_human"))


def allowed_variants(member: Dict[str, Any], *, public_ai: bool = False) -> Tuple[str, ...]:
    """TA 没说过话就只能从群切入；说过话三种都行。公开 AI 的人设另加「坦白是 AI」。"""
    base = OPENER_VARIANTS if str(member.get("last_msg_text") or "").strip() else ("group",)
    return base + (AI_INTRO_VARIANT,) if public_ai else base


def pick_variant(rates: Dict[str, Dict[str, int]], allowed: Sequence[str], *, seed: Any,
                 explore: float = AB_EXPLORE, min_sample: int = AB_MIN_SAMPLE,
                 prior: Optional[Dict[str, float]] = None,
                 prior_weight: float = AB_PRIOR_WEIGHT) -> str:
    """ε-greedy：每种都够样本且不在探索轮 → 平滑回复率最高的；否则先补样本最少的。

    平滑：(replied + w·prior) / (sent + w)，w=AB_PRIOR_WEIGHT——样本刚过线时先验仍有发言权，
    样本多了先验自然淡出。
    """
    allowed = tuple(allowed) or ("group",)
    if len(allowed) == 1:
        return allowed[0]
    rng = random.Random(str(seed))
    pri = dict(AB_PRIOR)
    pri.update(prior or {})
    scored = []
    for v in allowed:
        r = rates.get(v) or {}
        n = int(r.get("sent") or 0)
        if n >= int(min_sample):
            k = max(0.0, float(prior_weight))
            scored.append((v, (int(r.get("replied") or 0) + k * float(pri.get(v, 0.0))) / (n + k)))
    if len(scored) < len(allowed) or rng.random() < float(explore):
        return min(allowed, key=lambda v: (int((rates.get(v) or {}).get("sent") or 0), rng.random()))
    return max(scored, key=lambda vr: vr[1])[0]


def member_lang(member: Dict[str, Any], default: str = "") -> str:
    """对方该用什么语言：Telegram language_code > 他说的话里有没有汉字 > 默认。"""
    code = str(member.get("lang_code") or "").strip().lower()
    if code:
        base = code.split("-")[0].split("_")[0]
        if base in ("zh", "yue"):
            return "zh"
        if len(base) == 2:
            return base
    sample = "%s %s %s" % (member.get("last_msg_text") or "", member.get("first_name") or "",
                           member.get("group_title") or "")
    if _CJK_RE.search(sample):
        return "zh"
    return default or "en"


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(s or "")).lower()


def too_similar(text: str, others: Sequence[str], ceiling: float = SIMILARITY_CEILING) -> bool:
    """和今天已经发出去 / 已定稿的任何一条过于相像 → True。"""
    a = _norm(text)
    if not a:
        return False
    for o in others or []:
        b = _norm(o)
        if not b:
            continue
        if a == b:
            return True
        if difflib.SequenceMatcher(None, a, b).ratio() >= ceiling:
            return True
    return False


def language_mismatch(text: str, lang: str) -> bool:
    """中文要有汉字；非中文不该大半是汉字。粗判，只拦明显写错语言的。"""
    s = str(text or "")
    if not s.strip():
        return True
    cjk = len(_CJK_RE.findall(s))
    if lang == "zh":
        return cjk == 0
    letters = sum(1 for ch in s if ch.isalpha())
    return letters > 0 and cjk > letters // 2


def _first_line(raw: str) -> str:
    s = str(raw or "").strip()
    for line in s.splitlines():
        line = line.strip().strip(_QUOTE_CHARS).strip()
        if line and not line.lower().startswith(("开场", "opener", "回复", "reply")):
            return line
    return s.strip(_QUOTE_CHARS).strip()


def template_opener(member: Dict[str, Any], lang: str, *, seed: Any = None,
                    avoid: Sequence[str] = ()) -> str:
    """模板兜底。有他说过的话就引一句；几种变体轮着来，避开今天已用过的。"""
    name = str(member.get("first_name") or member.get("username") or "").strip()
    group = str(member.get("group_title") or "").strip()
    quote = " ".join(str(member.get("last_msg_text") or "").split())[:24]
    rng = random.Random(seed)
    pool: List[str] = []
    if lang == "zh":
        if quote:
            pool += list(_TEMPLATES_ZH_QUOTE)
        pool += list(_TEMPLATES_ZH)
    else:
        if quote:
            pool += list(_TEMPLATES_EN_QUOTE)
        pool += list(_TEMPLATES_EN)
    rng.shuffle(pool)
    fallback = ""
    for tpl in pool:
        try:
            text = tpl.format(name=name or ("朋友" if lang == "zh" else "there"),
                              group=group or ("群里" if lang == "zh" else "the group"),
                              quote=quote)
        except (KeyError, IndexError):
            continue
        text = text.replace("「群里」", "群里").replace(" in the group", " in the group")
        if not fallback:
            fallback = text
        if not too_similar(text, avoid):
            return text
    return fallback


def template_followup(member: Dict[str, Any], lang: str, *, seed: Any = None) -> str:
    name = str(member.get("first_name") or member.get("username") or "").strip()
    group = str(member.get("group_title") or "").strip()
    pool = list(_FOLLOWUP_ZH if lang == "zh" else _FOLLOWUP_EN)
    random.Random(seed).shuffle(pool)
    for tpl in pool:
        if "{group}" in tpl and not group:
            continue
        try:
            return tpl.format(name=name or ("朋友" if lang == "zh" else "there"), group=group)
        except (KeyError, IndexError):
            continue
    return pool[0].format(name=name or "朋友", group=group)


def compact_persona_block(persona: Optional[Dict[str, Any]], platform: str = "telegram") -> str:
    """开口 prompt 用的人设精简块：名字/角色/口吻/几句背景，不带全量规则。

    取不到结构化字段时回落到回复链全量格式器的开头一段（身份硬锁在最前）。
    """
    if not isinstance(persona, dict) or not persona:
        return ""
    try:
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        p = pm.normalize_profile_shape(persona)
        name = str(pm.resolve_spoken_name(p) or p.get("name") or "").strip()
    except Exception:
        p = dict(persona)
        name = str(p.get("name") or "").strip()
    lines: List[str] = []
    role = str(p.get("role") or "").strip()
    if name or role:
        lines.append("你是%s%s。" % (name or "这个人", ("，" + role) if role else ""))
    pers = p.get("personality") if isinstance(p.get("personality"), dict) else {}
    speak = p.get("speaking") if isinstance(p.get("speaking"), dict) else {}
    for src_d, keys in ((pers, ("style", "tone", "traits")),
                        (speak, ("style", "tone", "habits", "catchphrases"))):
        for k in keys:
            v = src_d.get(k)
            if isinstance(v, (list, tuple)):
                v = "、".join(str(x) for x in v if x)
            v = str(v or "").strip()
            if v:
                lines.append("- %s：%s" % ({"style": "说话风格", "tone": "语气", "traits": "性格",
                                           "habits": "习惯", "catchphrases": "口头禅"}[k], v[:120]))
    for k in ("background", "bio", "description", "summary"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            lines.append("- 背景：" + " ".join(v.split())[:200])
            break
    block = "\n".join(lines).strip()
    if len(block) < 20:
        try:
            from src.utils.persona_manager import PersonaManager
            full = PersonaManager.get_instance()._format_persona_instructions(
                persona, platform=platform)
            block = str(full or "")
        except Exception:
            block = ""
    return block[:PERSONA_BLOCK_MAX]


def postprocess_opener(raw: str, *, persona: Optional[Dict[str, Any]], lang: str,
                       avoid: Sequence[str]) -> Dict[str, str]:
    """LLM 原文 → 可发文案。返回 {text, reason}；reason 非空 = 这条不能用。"""
    text = _first_line(raw)
    if not text:
        return {"text": "", "reason": "empty"}
    if persona:
        try:
            from src.utils.persona_guard import sanitize
            text, _hits = sanitize(text, persona)
            text = str(text or "").strip()
        except Exception:
            logger.debug("[gm_opener] persona_guard 跳过", exc_info=True)
    if not persona_public_ai(persona):
        try:
            from src.utils.persona_guard import matches_ai_self_identity
            if matches_ai_self_identity(text):
                return {"text": text, "reason": "ai_self_id"}
        except Exception:
            pass
    block = opener_block_reason(text)
    if block:
        return {"text": text, "reason": block}
    limit = OPENER_MAX_CHARS_ZH if lang == "zh" else OPENER_MAX_CHARS_OTHER
    if len(text) > limit:
        return {"text": text, "reason": "too_long"}
    if language_mismatch(text, lang):
        return {"text": text, "reason": "lang"}
    if "在吗" in text or text.lower().startswith(("are you there", "r u there")):
        return {"text": text, "reason": "filler"}
    if too_similar(text, avoid):
        return {"text": text, "reason": "similar"}
    return {"text": text, "reason": ""}


def _identity_rule(public_ai: bool, extra: str = "") -> str:
    if public_ai:
        return ("不要问「在吗」，不要用「您」%s。你本来就是 AI，被问或切入需要时可以大方说，"
                "绝不冒充真人；不要自称客服、助理。" % extra)
    return "不要问「在吗」，不要用「您」，不要自称 AI、助理、客服%s。" % extra


def opener_prompt(*, persona_block: str, member: Dict[str, Any], lang: str,
                  goal_hint: str, avoid: Sequence[str], variant: str = "",
                  public_ai: bool = False) -> str:
    name = str(member.get("first_name") or member.get("username") or "对方").strip()
    uname = str(member.get("username") or "").strip()
    group = str(member.get("group_title") or "").strip()
    said = " ".join(str(member.get("last_msg_text") or "").split())[:200]
    lang_name = _LANG_NAMES.get(lang, lang)
    limit = OPENER_MAX_CHARS_ZH if lang == "zh" else OPENER_MAX_CHARS_OTHER
    parts: List[str] = []
    if persona_block:
        parts.append("你在 Telegram 上以下面这个人的身份说话，口吻、用词都要像TA：\n" + persona_block)
    parts.append("现在要给一个和你同在一个群、但没私聊过的人发**第一条**私信。")
    about = ["- 对方名字：%s%s" % (name, (" (@%s)" % uname) if uname else "")]
    if group:
        about.append("- 你们同在的群：%s" % group)
    if said:
        about.append("- TA 最近在群里说：「%s」" % said)
    parts.append("关于对方：\n" + "\n".join(about))
    if goal_hint:
        parts.append("这次建联的方向（只影响切入点和语气，第一条绝不能卖东西）：%s" % goal_hint)
    if variant in _VARIANT_HINT and (said or variant in ("group", AI_INTRO_VARIANT)):
        parts.append(_VARIANT_HINT[variant])
    rules = [
        "只写一句话，不超过 %d 个字符，用%s。" % (limit, lang_name),
        ("口语、随手打的感觉，不客套、不自我介绍一长串。" if public_ai else
         "像真人随手打的，不客套、不自我介绍一长串。"),
        ("如果TA说过话，优先接TA说的那句，让TA有话可回。" if said else
         "你不知道TA在群里说过什么、发过什么：别编造TA的发言、表情包、你们之间的往事或称呼，"
         "也别评论TA冒泡多少；可以聊这个群本身，或问一句和群主题相关的轻松问题。"),
        "不要介绍产品或服务，不要链接，不要让TA加微信/WhatsApp/别的联系方式。",
        _identity_rule(public_ai),
    ]
    if avoid:
        rules.append("下面这些是今天已经发给别人的，别写得像它们：\n" +
                     "\n".join("  · " + a for a in list(avoid)[:8]))
    parts.append("规则：\n" + "\n".join("- " + r for r in rules))
    parts.append("只输出这句话本身，不要引号，不要解释。")
    return "\n\n".join(parts)


def build_opener_context(cfg: Dict[str, Any], platform: str, account_id: str,
                         inbox_store: Any = None, *, registry: Any = None) -> Dict[str, Any]:
    """这个号用谁的人设、朝什么方向开口。取不到的项留空，调用方照常生成。"""
    out: Dict[str, Any] = {"persona_id": "", "persona_name": "", "persona": None,
                           "persona_block": "", "goal_template": "", "goal_name": "",
                           "goal_hint": "", "ab": ab_settings(cfg or {})}
    try:
        from src.ai.persona_voice import resolve_effective_persona
        pid, _tier = resolve_effective_persona(cfg or {}, platform, account_id, "",
                                               registry=registry)
        out["persona_id"] = str(pid or "")
    except Exception:
        logger.debug("[gm_opener] 人设解析跳过", exc_info=True)
    try:
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona_by_id(out["persona_id"]) if out["persona_id"] else None
        if isinstance(persona, dict) and persona:
            out["persona"] = persona
            out["persona_name"] = str(persona.get("name") or persona.get("id") or "")
            # 精简块（名字/角色/口吻/背景）：开口只要像这个人，不要整份回复规则
            out["persona_block"] = compact_persona_block(persona, platform)
    except Exception:
        logger.debug("[gm_opener] 人设块装配跳过", exc_info=True)
    try:
        from src.companion.member_intent import intent_terms
        out["intent"] = intent_terms(out.get("persona"), cfg or {})
    except Exception:
        out["intent"] = {"positive": [], "negative": []}
    try:
        from src.companion.goals import defaults as gdef
        from src.companion.goals.templates import get_template
        spec = gdef.get_default(inbox_store, platform=platform, account_id=account_id,
                                persona_id=out["persona_id"]) if inbox_store is not None else None
        tid = str((spec or {}).get("template") or "")
        tmpl = get_template(tid) if tid else None
        if tmpl:
            out["goal_template"] = tid
            out["goal_name"] = str(tmpl.get("name_zh") or tid)
            # 方向只给「目标名 + 主题物」，不把阶段意图整句塞进去——第一条不卖东西，
            # 意图池里「讲价格 / 怎么解锁」那些拍子属于回复链，不属于开口。
            params = (spec or {}).get("params") or {}
            item = str(params.get("item_label") or params.get("product_name") or "").strip()
            out["goal_hint"] = out["goal_name"] + (("（围绕：%s）" % item) if item else "")
    except Exception:
        logger.debug("[gm_opener] 默认目标读取跳过", exc_info=True)
    return out


async def compose_opener(ai: Any, *, member: Dict[str, Any], ctx: Dict[str, Any],
                         avoid: Sequence[str] = (), default_lang: str = "",
                         attempts: int = 2, variant: str = "") -> Dict[str, Any]:
    """生成一条可发的开场。返回 {text, source, lang, reason, variant}。

    ``ai`` 是 ``AIClient``（有 ``chat(prompt)``）；None 或失败 → 模板。
    source: ai / template。reason 记最后一次 AI 文案被拒的原因（可观测）。
    ``variant`` 是切入方式（见 OPENER_VARIANTS）；模板稿不分切入方式，回 ''。
    """
    lang = member_lang(member, default_lang)
    persona = ctx.get("persona") if isinstance(ctx, dict) else None
    last_reason = ""
    if ai is not None and hasattr(ai, "chat"):
        avoid_now = list(avoid)
        for _ in range(max(1, int(attempts))):
            prompt = opener_prompt(
                persona_block=str(ctx.get("persona_block") or ""), member=member,
                lang=lang, goal_hint=str(ctx.get("goal_hint") or ""), avoid=avoid_now,
                variant=variant, public_ai=persona_public_ai(persona))
            try:
                try:
                    from src.ai.llm_purpose import purpose_scope
                except ImportError:  # pragma: no cover
                    from contextlib import nullcontext as purpose_scope  # type: ignore
                with purpose_scope("customer_reply"):
                    raw = await ai.chat(prompt)
            except Exception:
                logger.debug("[gm_opener] LLM 调用失败", exc_info=True)
                raw = ""
            got = postprocess_opener(str(raw or ""), persona=persona, lang=lang, avoid=avoid)
            if not got["reason"]:
                return {"text": got["text"], "source": "ai", "lang": lang, "reason": "",
                        "variant": variant}
            last_reason = got["reason"]
            if got["text"]:
                avoid_now = avoid_now + [got["text"]]
    text = template_opener(member, lang, seed=str(member.get("user_id") or ""), avoid=avoid)
    return {"text": text, "source": "template", "lang": lang, "reason": last_reason,
            "variant": ""}


def followup_prompt(*, persona_block: str, member: Dict[str, Any], lang: str,
                    public_ai: bool = False) -> str:
    name = str(member.get("first_name") or member.get("username") or "对方").strip()
    group = str(member.get("group_title") or "").strip()
    first = " ".join(str(member.get("opener_text") or "").split())[:200]
    lang_name = _LANG_NAMES.get(lang, lang)
    limit = OPENER_MAX_CHARS_ZH if lang == "zh" else OPENER_MAX_CHARS_OTHER
    parts: List[str] = []
    if persona_block:
        parts.append("你在 Telegram 上以下面这个人的身份说话，口吻、用词都要像TA：\n" + persona_block)
    parts.append("三天前你给同群的 %s 发过第一条私信，TA 没回。现在补**唯一一次**跟进。" % name)
    said = " ".join(str(member.get("last_msg_text") or "").split())[:200]
    about = []
    if group:
        about.append("- 你们同在的群：%s" % group)
    if said:
        about.append("- TA 之前在群里说过：「%s」（可以换个角度接这句，别照搬上次的接法）" % said)
    if first:
        about.append("- 你上次发的是：「%s」" % first)
    if about:
        parts.append("\n".join(about))
    rules = [
        "只写一句话，不超过 %d 个字符，用%s。" % (limit, lang_name),
        "轻描淡写地提一下上次那条，给TA台阶（不方便聊也没事），不追问为什么没回。",
        "不要重复上次的话，不要介绍产品或服务，不要链接，不要让TA加别的联系方式。",
        _identity_rule(public_ai, "，不要道歉式开头"),
    ]
    parts.append("规则：\n" + "\n".join("- " + r for r in rules))
    parts.append("只输出这句话本身，不要引号，不要解释。")
    return "\n\n".join(parts)


async def compose_followup(ai: Any, *, member: Dict[str, Any], ctx: Dict[str, Any],
                           default_lang: str = "") -> Dict[str, Any]:
    """72h 跟进文案。同一套出站过滤；和上次开口雷同也算不过。LLM 不在 → 模板。"""
    lang = member_lang(member, default_lang)
    persona = ctx.get("persona") if isinstance(ctx, dict) else None
    avoid = [str(member.get("opener_text") or "")]
    last_reason = ""
    if ai is not None and hasattr(ai, "chat"):
        prompt = followup_prompt(persona_block=str(ctx.get("persona_block") or ""),
                                 member=member, lang=lang,
                                 public_ai=persona_public_ai(persona))
        try:
            try:
                from src.ai.llm_purpose import purpose_scope
            except ImportError:  # pragma: no cover
                from contextlib import nullcontext as purpose_scope  # type: ignore
            with purpose_scope("customer_reply"):
                raw = await ai.chat(prompt)
        except Exception:
            logger.debug("[gm_opener] 跟进 LLM 调用失败", exc_info=True)
            raw = ""
        got = postprocess_opener(str(raw or ""), persona=persona, lang=lang, avoid=avoid)
        if not got["reason"]:
            return {"text": got["text"], "source": "ai", "lang": lang, "reason": ""}
        last_reason = got["reason"]
    text = template_followup(member, lang, seed=str(member.get("user_id") or ""))
    return {"text": text, "source": "template", "lang": lang, "reason": last_reason}


async def compose_queue(store: Any, ai: Any, *, account_id: str, ctx: Dict[str, Any],
                        since_ts: float, force: bool = False,
                        pairs: Optional[Sequence[Any]] = None,
                        default_lang: str = "", reset_manual: bool = False) -> Dict[str, Any]:
    """给这个号队列里（queued/approved）还没文案的人逐个拟稿并落库。

    ``force`` 重拟已有 ai/template 文案的；坐席手改过的（manual）默认永不覆盖，
    只有 ``reset_manual`` 且点名 ``pairs`` 时才放弃手改回到 AI 稿（坐席在卡上明确点的）。
    ``pairs`` 给了只拟这些 (group_id, user_id)。
    返回 {composed, ai, template, skipped, items:[{group_id,user_id,text,source}]}。
    """
    from src.companion.group_members_store import OUTREACH_APPROVED, OUTREACH_QUEUED
    want = {(str(g), str(u)) for g, u in (pairs or [])} if pairs else None
    rows = [m for m in store.list_by_hash_account(account_id)
            if str(m.get("outreach_state") or "") in (OUTREACH_QUEUED, OUTREACH_APPROVED)]
    avoid = list(store.today_opener_texts(account_id, since_ts))
    # 切入方式 A/B：近 30 天这个号各切入的回复率；取不到就当没样本（全探索）
    try:
        rates = store.variant_rates(float(since_ts) - AB_LOOKBACK_SEC, account_id) or {}
    except Exception:
        rates = {}
    out: Dict[str, Any] = {"composed": 0, "ai": 0, "template": 0, "skipped": 0, "items": [],
                           "variants": {}}
    for m in rows:
        key = (str(m.get("group_id")), str(m.get("user_id")))
        if want is not None and key not in want:
            continue
        src = str(m.get("opener_source") or "")
        keep_manual = src == "manual" and not (reset_manual and want is not None)
        if m.get("opener_text") and (keep_manual or not force):
            out["skipped"] += 1
            continue
        own = str(m.get("opener_text") or "")
        avoid_others = [a for a in avoid if a != own] if own else avoid
        ab = ctx.get("ab") if isinstance(ctx.get("ab"), dict) else {}
        variant = pick_variant(rates, allowed_variants(m, public_ai=persona_public_ai(
                                   ctx.get("persona"))), seed="%s|%s" % (account_id, key[1]),
                               explore=float(ab.get("explore", AB_EXPLORE)),
                               min_sample=int(ab.get("min_sample", AB_MIN_SAMPLE)),
                               prior=ab.get("prior"),
                               prior_weight=float(ab.get("prior_weight", AB_PRIOR_WEIGHT)))
        got = await compose_opener(ai, member=m, ctx=ctx, avoid=avoid_others,
                                   default_lang=default_lang, variant=variant)
        text = str(got.get("text") or "").strip()
        if not text:
            out["skipped"] += 1
            continue
        used_variant = str(got.get("variant") or "")
        if store.set_opener(key[0], key[1], text, got["source"],
                            persona_id=str(ctx.get("persona_id") or ""), variant=used_variant):
            out["composed"] += 1
            out[got["source"]] = out.get(got["source"], 0) + 1
            if used_variant:
                out["variants"][used_variant] = out["variants"].get(used_variant, 0) + 1
            avoid.append(text)
            out["items"].append({"group_id": key[0], "user_id": key[1], "text": text,
                                 "source": got["source"], "reason": got.get("reason", ""),
                                 "variant": used_variant})
    return out


__all__ = [
    "OPENER_MAX_CHARS_ZH", "OPENER_MAX_CHARS_OTHER", "SIMILARITY_CEILING",
    "member_lang", "too_similar", "language_mismatch", "template_opener",
    "postprocess_opener", "opener_prompt", "build_opener_context", "compose_opener",
    "compose_queue", "compact_persona_block", "template_followup", "followup_prompt",
    "compose_followup", "PERSONA_BLOCK_MAX",
    "OPENER_VARIANTS", "AB_EXPLORE", "AB_MIN_SAMPLE", "AB_LOOKBACK_SEC", "AB_PRIOR",
    "AB_PRIOR_WEIGHT", "ab_settings", "allowed_variants", "pick_variant",
    "AI_INTRO_VARIANT", "ALL_VARIANTS", "persona_public_ai",
]
