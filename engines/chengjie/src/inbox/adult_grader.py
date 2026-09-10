# -*- coding: utf-8 -*-
"""成人内容分级 + 拦后软回应（Q-15 #271，2026-09-10）。

背景（tmp_diag Z25RQS / 9JP5SZ）：``chat_assistant_service._RISK_TERMS["adult"]`` 命中一个词
（nudes / sex / 裸照…）就 ``risk=high`` → autosend_policy 判 L1 人审 + 「需人工」标 → 全自动
会话当场哑火。开黄腔、玩笑式一句「sexy」和「send nudes now」是两种事，此前一刀切。

本模块做三件事，全部 best-effort、任何异常按「不改判定、不发」放行：

A. :func:`grade` —— 入站四级：``mention``（单个成人词、无施压）/ ``flirt``（成人词 + 玩笑
   口吻 / 两个成人词）/ ``explicit``（露骨词：器官 / 行为 / 裸照 / 色情内容）/ ``pressure``
   （露骨 + 催促施压）。三语词表（en / zh / ja）+ 施压词。

B. :func:`adult_policy_of` —— 人设 ``boundaries.adult_policy``：``human``（拦下转人工，现状）/
   ``soft_reply``（陪聊域默认：命中即发一句人设口吻软回应，不沉默）/ ``mark_only``（只标不转）。
   未显式配置 → 业务域 companion（business_domain）→ soft_reply，其余（销售 / 客服）→ human。

C. :func:`regrade_inbound`（drafts.py 唯一钩子）——
   · mention / flirt → ``risk=medium``、reasons 首位 ``adult_flirt``（日志 shadow=adult_flirt）、
     **不** needs_human、不发软回应（各政策一致）；
   · explicit → 按政策：human → high + needs_human（reason ``adult:explicit:<hit>``）+ 3 分钟无人
     接手补发一次软回应；soft_reply → high + 立即软回应 + needs_human（Q-3 闸拦后续自动稿）；
     mark_only → medium + ``adult_mark`` 标记，不转不发；
   · pressure → 一律 high + needs_human（安全地板，不看 mark_only）；soft_reply 立即发；human
     3 分钟补发。
   软回应经 DraftService 的人工通过投递回调（``AutosendWorker.deliver_human_approved``：不过
   人工优先闸 / 不进 pacing——needs_human 在场时 L2 稿会被 Q-3 闸取消，故不能当 L2 稿发）。
   软回应**不是**缓冲句、不做全局兜底：只在 explicit / pressure × (soft_reply | human 超时) 出。

日志：``[adult] grade conv= level= hits= policy=`` / ``[adult] soft_reply conv= level= policy= mode=``。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import weakref
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("ai_chat_assistant.adult_grader")

CATEGORY = "adult"
LEVELS: Tuple[str, ...] = ("mention", "flirt", "explicit", "pressure")
POLICIES: Tuple[str, ...] = ("human", "soft_reply", "mark_only")
DEFAULT_POLICY_COMPANION = "soft_reply"
DEFAULT_POLICY_OTHER = "human"
FOLLOWUP_SEC = 180.0
KV_PREFIX = "adult_soft:"          # 每会话账本 {ts, level, policy, mode, tag_ts}
REASON_PREFIX = "adult:"           # 打标 / risk_reasons 标签：adult:<level>[:<hit>]
FLIRT_REASON = "adult_flirt"
MARK_REASON = "adult_mark"

_LB = r"(?<![A-Za-z])"
_RB = r"(?![A-Za-z])"


def _en(words: Sequence[str]) -> "re.Pattern[str]":
    return re.compile(_LB + "(?:" + "|".join(words) + ")" + _RB, re.IGNORECASE)


def _cjk(words: Sequence[str]) -> "re.Pattern[str]":
    return re.compile("(?:" + "|".join(re.escape(w) for w in words) + ")")


# ── 词表 ────────────────────────────────────────────────────────────────────
# mention：单独出现只算「提到 / 撩」——sex 一词、性感、亲亲、上床等轻度词
_MENTION = [
    _en(["sex", "sexy", "sexting", "horny", "hot body", "turn(?:s|ed)? me on", "kiss(?:es|ing)? you",
         "in bed", "make out", "sleep with (?:you|me)", "naughty", "kinky", "seduc(?:e|tive)",
         "strip", "lingerie", "bikini pics?", "thirsty", "dirty talk", "hookup"]),
    _cjk(["性感", "色色", "开黄腔", "黄段子", "上床", "睡你", "睡我", "亲亲", "亲一个", "想亲你",
          "调情", "撩我", "撩你", "湿了", "硬了", "骚", "污", "内衣", "睡衣照", "约炮", "一夜情",
          "打炮", "开房", "情趣"]),
    _cjk(["セクシー", "エッチ", "キスして", "キスしたい", "いやらしい", "ムラムラ", "エロい",
          "下着", "水着", "ヤリたい", "寝たい", "一夜"]),
]
# explicit：器官 / 性行为 / 裸照 / 色情内容——单词即露骨
_EXPLICIT = [
    _en(["nudes?", "naked", "nude pics?", "porn(?:o|ography)?", "onlyfans", "dick(?: pic)?", "cock",
         "pussy", "boobs?", "tits", "titties", "nipples?", "blow ?job", "hand ?job", "cum(?:ming|shot)?",
         "jerk(?:ing)? off", "masturbat(?:e|ing|ion)", "fuck(?:ing)? (?:you|me)", "fuck me",
         "suck (?:my|your|it)", "anal", "orgasm", "wet pussy", "hard on", "boner", "send (?:me )?(?:a )?pic of your (?:body|chest|ass)",
         "show me your (?:body|boobs?|tits|ass|pussy|dick)", "cam ?sex", "video ?sex", "phone sex",
         "sex(?:ual)? video", "spread your legs", "cyber ?sex"]),
    _cjk(["裸照", "裸体", "全裸", "脱光", "脱衣", "裸聊", "视频裸", "做爱", "性交", "口交", "肛交",
          "自慰", "打飞机", "射精", "高潮", "阴茎", "阴道", "鸡巴", "鸡鸡", "奶子", "胸照", "下面湿",
          "小穴", "阴部", "成人视频", "色情", "黄片", "毛片", "A片", "av片", "看你的胸", "看看你的身体",
          "给我看你的", "内裤照", "脱了", "操你", "干你", "插你", "舔你", "摸你的", "摸摸胸", "揉胸", "肉体"]),
    _cjk(["セックス", "ヌード", "裸写真", "全裸", "裸", "おっぱい", "乳首", "ちんこ", "ちんちん",
          "まんこ", "アダルト", "エロ動画", "エロ画像", "オナニー", "自慰", "射精", "イク", "フェラ",
          "手コキ", "性行為", "挿入", "ビデオ通話で脱", "脱いで", "見せて.*(?:胸|体|下着|裸)", "パンツ見せ",
          "抱きたい.*裸", "ヤろう", "ヤらせ", "エッチしよ", "エッチな写真", "エッチな動画"]),
]
# pressure：催促 / 命令 / 施压——与 explicit 同现才成 pressure
_PRESSURE = [
    _en(["now", "right now", "tonight", "hurry", "quick(?:ly)?", "come on", "c'?mon", "send (?:it|them|me|one)",
         "send me", "show me", "give me", "i want you to", "you have to", "you must", "you need to",
         "do it", "don'?t be (?:shy|a tease)", "stop being shy", "or else", "or i'?ll", "if you don'?t",
         "prove it", "just do it", "please+", "why not", "don'?t make me wait", "i'?m waiting", "asap"]),
    _cjk(["现在", "马上", "立刻", "快点", "快发", "快给", "赶紧", "今晚", "必须", "一定要", "发给我", "发过来",
          "给我看", "让我看", "给我发", "不然", "否则", "别装", "别害羞", "别矫情", "不要磨叽", "我等着",
          "等你发", "求你", "拜托", "就一张", "就一次", "不发就", "不给就", "证明"]),
    _cjk(["今すぐ", "早く", "はやく", "急いで", "今夜", "送って", "見せて", "みせて", "ちょうだい", "くれ",
          "しなさい", "しろ", "じゃないと", "でなければ", "恥ずかしがらない", "照れないで", "待ってる",
          "お願い", "一枚だけ", "一回だけ", "証明"]),
]
# flirt 口吻：笑 / 调侃 / 挑逗 emoji——mention 命中 + 这些 → flirt
_FLIRT_TONE = re.compile(
    r"(?:haha|lol|lmao|jk|just kidding|kidding|tease|teasing|;\)|:p|😏|😘|😉|😜|🙈|🥵|🔥|哈哈|嘿嘿|"
    r"开玩笑|逗你|皮一下|坏笑|www|笑|冗談|ふふ|てへ|〜|~)",
    re.IGNORECASE,
)


def sniff_lang(text: str, lang: Optional[str] = None) -> str:
    try:
        from src.inbox.commitment_guard import sniff_lang as _sl
        return _sl(text, lang)
    except Exception:
        return "en"


def _hits(pats: Sequence["re.Pattern[str]"], text: str, limit: int = 6) -> List[str]:
    out: List[str] = []
    for p in pats:
        for m in p.finditer(text):
            h = str(m.group(0) or "").strip()
            if h and h.lower() not in [x.lower() for x in out]:
                out.append(h)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # 子串去重（ja「裸」落在 zh「裸照」里）：留最长那个
    return [h for h in out if not any(h != o and h.lower() in o.lower() for o in out)]


def grade(text: str, lang: Optional[str] = None) -> Dict[str, Any]:
    """返回 ``{level, hits, pressure_hits, category, lang}``；无命中 → ``level=""``。

    分级（只看这一条入站，不看历史）：
      explicit 词 + pressure 词 → pressure；explicit 词 → explicit；
      仅 mention 词：一个 + 调侃口吻 / ≥2 个 → flirt，否则 mention；≥3 个 mention 词 → explicit。
    """
    t = str(text or "").strip()
    out: Dict[str, Any] = {"level": "", "hits": [], "pressure_hits": [], "category": CATEGORY,
                           "lang": sniff_lang(t, lang)}
    if not t:
        return out
    low = t.lower()
    exp = _hits(_EXPLICIT, low)
    men = [h for h in _hits(_MENTION, low) if h.lower() not in [e.lower() for e in exp]]
    pres = _hits(_PRESSURE, low)
    if exp:
        out["hits"] = exp + men
        out["pressure_hits"] = pres
        out["level"] = "pressure" if pres else "explicit"
        return out
    if not men:
        return out
    out["hits"] = men
    out["pressure_hits"] = pres
    if len(men) >= 3:
        out["level"] = "explicit"
    elif len(men) >= 2 or _FLIRT_TONE.search(t):
        out["level"] = "flirt"
    else:
        out["level"] = "mention"
    return out


def is_blocking_level(level: str) -> bool:
    return str(level or "") in ("explicit", "pressure")


# ── 人设政策 ────────────────────────────────────────────────────────────────

def normalize_policy(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    aliases = {"human": "human", "handoff": "human", "转人工": "human", "soft": "soft_reply",
               "soft_reply": "soft_reply", "软回应": "soft_reply", "mark": "mark_only",
               "mark_only": "mark_only", "只标": "mark_only", "只标不转": "mark_only"}
    return aliases.get(s, "")


def default_policy(cfg: Any = None) -> str:
    """无显式配置时的域默认：陪聊域 soft_reply，销售 / 客服域 human。"""
    try:
        from src.utils.business_domain import COMPANION, active_business_domain
        return DEFAULT_POLICY_COMPANION if active_business_domain(cfg) == COMPANION else DEFAULT_POLICY_OTHER
    except Exception:
        return DEFAULT_POLICY_OTHER


def adult_policy_of(persona: Any, cfg: Any = None) -> Tuple[str, str]:
    """``(policy, source)``；source ∈ persona / default_companion / default。"""
    try:
        b = (persona or {}).get("boundaries") if isinstance(persona, dict) else None
        p = normalize_policy((b or {}).get("adult_policy"))
        if p:
            return p, "persona"
    except Exception:
        pass
    d = default_policy(cfg)
    return d, ("default_companion" if d == DEFAULT_POLICY_COMPANION else "default")


def resolve_persona(conv: Dict[str, Any], cfg: Any = None) -> Any:
    """与 commitment_guard.evaluate_inbound 同路：账号 / 会话有效人设。拿不到 → None。"""
    try:
        plat = str((conv or {}).get("platform") or "")
        acct = str((conv or {}).get("account_id") or "")
        ck = str((conv or {}).get("chat_key") or "")
        if not (plat or acct or ck):
            return None
        from src.ai.persona_voice import resolve_effective_persona_id
        pid = str(resolve_effective_persona_id(cfg or {}, plat, acct, ck) or "")
        if not pid:
            return None
        from src.utils.persona_manager import PersonaManager
        return PersonaManager.get_instance().get_persona_by_id(pid)
    except Exception:
        return None


def prompt_block(persona: Any, *, compact: bool = False) -> str:
    """人设 prompt 段（persona_manager 注入）。显式政策才带专名段，缺省不占字。"""
    try:
        b = (persona or {}).get("boundaries") if isinstance(persona, dict) else None
        p = normalize_policy((b or {}).get("adult_policy"))
    except Exception:
        p = ""
    if p == "mark_only":
        return ("【成人话题·只标记】对方开黄腔或露骨：按人设自然回应，不迎合露骨要求，不说教，"
                "一句带过换话题。")
    if p == "soft_reply":
        return ("【成人话题·软回应】对方开黄腔或露骨：既不迎合也不生硬拒绝，一句轻带过换话题；"
                "被追问施压不接话。")
    if p == "human":
        return ("【成人话题·转人工】对方开黄腔：轻轻带过换话题；露骨或施压时不接话，交人工处理。")
    return ""


# ── 软回应话术库（人设口吻：不迎合、不生硬、不客服腔；按拒绝口吻 soft / direct）─────

_SOFT_LINES: Dict[str, Dict[str, List[str]]] = {
    "zh": {
        "soft": [
            "哈哈你这话题跳得也太快了，我脸都热了。先聊点别的呗，我还想多了解你一点。",
            "嗯……这个我不太想在这儿聊。你今天过得怎么样，说点别的给我听？",
            "你这样说我有点不知道怎么接了。我们慢慢来嘛，先聊聊别的？",
        ],
        "direct": [
            "这个话题我不聊。想聊别的我在。",
            "打住，这个不行。换个话题吧。",
        ],
    },
    "en": {
        "soft": [
            "Haha okay, that escalated fast. I'm not going there — tell me something else about your day?",
            "Mm, I'd rather not get into that here. Let's keep it light, what've you been up to?",
            "You're making me blush and not in a good way, haha. Let's talk about something else?",
        ],
        "direct": [
            "Not going there. Happy to keep chatting about other stuff though.",
            "That's a no from me. Different topic?",
        ],
    },
    "ja": {
        "soft": [
            "ちょっと、話が飛びすぎ（笑）そっちの話はなしで。今日は何してたの？",
            "うーん、それはここでは話したくないな。別の話しよ？",
            "そういうのはちょっと困るかも。もっと普通のこと聞かせて？",
        ],
        "direct": [
            "その話はしないよ。他の話ならいいけど。",
            "それはなし。別の話題にしよ。",
        ],
    },
}


def soft_reply_candidates(lang: str, style: str = "soft") -> List[str]:
    bank = _SOFT_LINES.get(str(lang or "en")) or _SOFT_LINES["en"]
    return list(bank.get(style if style in ("soft", "direct") else "soft") or bank["soft"])


def pick_soft_reply(lang: str, *, style: str = "soft", seed: str = "") -> str:
    """确定性挑一句（seed=会话 id + 日期）——同会话同日不换句，跨会话不撞同一句。"""
    cands = soft_reply_candidates(lang, style)
    if not cands:
        return ""
    if not seed:
        seed = str(int(time.time() // 86400))
    idx = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % len(cands)
    return cands[idx]


def soft_reply_for(persona: Any, lang: str, *, cid: str = "", now: Optional[float] = None) -> str:
    style = "soft"
    try:
        from src.inbox.commitment_guard import commitment_style_of
        style = commitment_style_of(persona)
    except Exception:
        style = "soft"
    day = str(int(float(now if now is not None else time.time()) // 86400))
    return pick_soft_reply(lang, style=style, seed=f"{cid}|{day}")


# ── 账本（KV：adult_soft:<cid>）──────────────────────────────────────────────

def _kv_get(store: Any, cid: str) -> Dict[str, Any]:
    if not cid or store is None or not hasattr(store, "get_app_setting"):
        return {}
    try:
        raw = store.get_app_setting(KV_PREFIX + cid, "") or ""
        rec = json.loads(raw) if raw else {}
        return rec if isinstance(rec, dict) else {}
    except Exception:
        return {}


def _kv_set(store: Any, cid: str, rec: Dict[str, Any]) -> None:
    if not cid or store is None or not hasattr(store, "set_app_setting"):
        return
    try:
        store.set_app_setting(KV_PREFIX + cid, json.dumps(rec, ensure_ascii=False),
                              updated_by="adult_grader")
    except TypeError:
        try:
            store.set_app_setting(KV_PREFIX + cid, json.dumps(rec, ensure_ascii=False))
        except Exception:
            logger.debug("[adult] 账本写入失败（忽略）", exc_info=True)
    except Exception:
        logger.debug("[adult] 账本写入失败（忽略）", exc_info=True)


def last_soft_reply(store: Any, cid: str) -> Dict[str, Any]:
    return _kv_get(store, cid)


# ── 发送：经 DraftService 人工通过投递回调（不过人工优先闸）────────────────────

_SVC_REF: Any = None            # weakref.ref → DraftService（drafts.py 钩子首次调用时绑定）
_TIMERS: Dict[str, Any] = {}    # cid → threading.Timer（3 分钟补发，进程内）
_TIMERS_LOCK = threading.Lock()


def bind_service(svc: Any) -> None:
    global _SVC_REF
    if svc is None:
        return
    try:
        _SVC_REF = weakref.ref(svc)
    except TypeError:
        _SVC_REF = lambda: svc  # noqa: E731（不可弱引用的桩对象）


def _bound_service() -> Any:
    try:
        return _SVC_REF() if _SVC_REF is not None else None
    except Exception:
        return None


def _deliver_cb(svc: Any) -> Any:
    return getattr(svc, "_inbox_deliver_cb", None) if svc is not None else None


def _find_loop(cb: Any) -> Any:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass
    owner = getattr(cb, "__self__", None)
    loop = getattr(owner, "_loop", None)
    try:
        if loop is not None and loop.is_running():
            return loop
    except Exception:
        pass
    return None


def dispatch_soft_reply(conv: Dict[str, Any], text: str, *, svc: Any = None,
                        level: str = "", policy: str = "", mode: str = "immediate",
                        now: Optional[float] = None) -> str:
    """把软回应排进人工通过投递链。返回 ``scheduled`` / ``no_deliver_cb`` / ``no_loop`` / ``empty``。

    只排队不等结果——投递回调自吞异常并负责失败审计 + 坐席铃铛。
    """
    cid = str((conv or {}).get("conversation_id") or "")
    text = str(text or "").strip()
    if not text or not cid:
        return "empty"
    svc = svc if svc is not None else _bound_service()
    cb = _deliver_cb(svc)
    if cb is None:
        logger.warning("[adult] soft_reply conv=%s level=%s policy=%s mode=%s status=no_deliver_cb",
                       cid, level or "-", policy or "-", mode)
        return "no_deliver_cb"
    ts = float(now if now is not None else time.time())
    row = {
        "draft_id": f"adult_soft:{cid}:{int(ts)}",
        "conversation_id": cid,
        "platform": str(conv.get("platform") or ""),
        "account_id": str(conv.get("account_id") or "default"),
        "chat_key": str(conv.get("chat_key") or ""),
        "final_text": text,
        "draft_text": text,
        "created_ts": ts,
    }
    loop = _find_loop(cb)
    if loop is None:
        logger.warning("[adult] soft_reply conv=%s level=%s policy=%s mode=%s status=no_loop",
                       cid, level or "-", policy or "-", mode)
        return "no_loop"
    try:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(cb(dict(row)))
        else:
            asyncio.run_coroutine_threadsafe(cb(dict(row)), loop)
    except Exception:
        logger.warning("[adult] soft_reply conv=%s 排入投递失败", cid, exc_info=True)
        return "no_loop"
    logger.info("[adult] soft_reply conv=%s level=%s policy=%s mode=%s text=%s",
                cid, level or "-", policy or "-", mode, text[:60])
    return "scheduled"


def send_soft_reply(store: Any, conv: Dict[str, Any], *, level: str, policy: str, mode: str,
                    persona: Any = None, lang: str = "", cfg: Any = None, svc: Any = None,
                    now: Optional[float] = None, tag_ts: float = 0.0,
                    peer_text: str = "") -> Dict[str, Any]:
    """挑句 + 排队 + 记账。返回 ``{status, text}``。语言：显式 lang → 入站文本嗅探 → en。"""
    cid = str((conv or {}).get("conversation_id") or "")
    ts = float(now if now is not None else time.time())
    if persona is None:
        persona = resolve_persona(conv, cfg)
    text = soft_reply_for(persona, sniff_lang(peer_text, lang), cid=cid, now=ts)
    status = dispatch_soft_reply(conv, text, svc=svc, level=level, policy=policy, mode=mode, now=ts)
    if status == "scheduled":
        _kv_set(store, cid, {"ts": ts, "level": level, "policy": policy, "mode": mode,
                             "tag_ts": float(tag_ts or ts), "text": text[:120]})
    return {"status": status, "text": text}


# ── 3 分钟无人接手补发（policy=human）─────────────────────────────────────────

def _agent_replied_since(store: Any, cid: str, since_ts: float) -> bool:
    try:
        rows = store.list_recent_messages(cid, limit=30) or []
    except Exception:
        return False
    for m in rows:
        try:
            if not str(m.get("direction") or "").startswith("out"):
                continue
            if str(m.get("status") or "") in ("failed", "resent"):
                continue
            if float(m.get("ts") or 0) > since_ts:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _still_needs_human(store: Any, cid: str) -> bool:
    try:
        from src.integrations.protocol_autoreply import HANDOFF_TAG
        return HANDOFF_TAG in list(store.get_conv_tags(cid) or [])
    except Exception:
        return False


def run_followup(store: Any, conv: Dict[str, Any], *, level: str, tag_ts: float,
                 lang: str = "", cfg: Any = None, svc: Any = None,
                 now: Optional[float] = None) -> str:
    """到点判定 + 补发一次。返回 skip 原因或 ``sent``。

    跳过：标已摘（人已接手 / 摘标）/ 打标后有出站（人已回）/ 本次打标已补过 / 会话已冻结。
    """
    cid = str((conv or {}).get("conversation_id") or "")
    ts = float(now if now is not None else time.time())
    with _TIMERS_LOCK:
        _TIMERS.pop(cid, None)
    if store is None or not cid:
        return "no_store"
    if not _still_needs_human(store, cid):
        logger.info("[adult] followup conv=%s skip=tag_cleared", cid)
        return "tag_cleared"
    if _agent_replied_since(store, cid, float(tag_ts or 0)):
        logger.info("[adult] followup conv=%s skip=agent_replied", cid)
        return "agent_replied"
    try:
        from src.inbox.stop_contact import frozen_reason
        if frozen_reason(store, cid):
            return "frozen"
    except Exception:
        pass
    prev = _kv_get(store, cid)
    try:
        if prev and float(prev.get("ts") or 0) >= float(tag_ts or 0) > 0:
            logger.info("[adult] followup conv=%s skip=already_sent", cid)
            return "already_sent"
    except (TypeError, ValueError):
        pass
    res = send_soft_reply(store, conv, level=level, policy="human", mode="followup",
                          lang=lang, cfg=cfg, svc=svc, now=ts, tag_ts=tag_ts)
    return "sent" if res.get("status") == "scheduled" else str(res.get("status") or "failed")


def schedule_followup(store: Any, conv: Dict[str, Any], *, level: str, tag_ts: float,
                      lang: str = "", cfg: Any = None, svc: Any = None,
                      delay: Optional[float] = None) -> bool:
    """登记 3 分钟后补发定时器（同会话只保留一枚；进程重启即失效——补发是尽力而为）。"""
    cid = str((conv or {}).get("conversation_id") or "")
    if not cid or store is None:
        return False
    d = float(FOLLOWUP_SEC if delay is None else delay)
    payload = dict(conv)
    svc = svc if svc is not None else _bound_service()

    def _fire() -> None:
        try:
            run_followup(store, payload, level=level, tag_ts=tag_ts, lang=lang, cfg=cfg, svc=svc)
        except Exception:
            logger.debug("[adult] followup 执行异常（忽略）", exc_info=True)

    t = threading.Timer(d, _fire)
    t.daemon = True
    with _TIMERS_LOCK:
        old = _TIMERS.pop(cid, None)
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        _TIMERS[cid] = t
    t.start()
    logger.info("[adult] followup scheduled conv=%s level=%s in=%.0fs", cid, level, d)
    return True


def pending_followups() -> List[str]:
    with _TIMERS_LOCK:
        return sorted(_TIMERS.keys())


def cancel_followup(cid: str) -> bool:
    with _TIMERS_LOCK:
        t = _TIMERS.pop(str(cid or ""), None)
    if t is None:
        return False
    try:
        t.cancel()
    except Exception:
        pass
    return True


# ── 打标钩子（protocol_autoreply.tag_needs_human 唯一调用口）───────────────────

def parse_reason(reason: Any) -> Tuple[str, str]:
    """``adult:<level>[:<hit>]`` → ``(level, hit)``；非本类 → ``("", "")``。"""
    s = str(reason or "")
    if not s.startswith(REASON_PREFIX):
        return "", ""
    parts = s.split(":", 2)
    level = parts[1] if len(parts) > 1 else ""
    hit = parts[2] if len(parts) > 2 else ""
    return (level if level in LEVELS else ""), hit


def on_needs_human_tagged(store: Any, cid: str, reason: Any, payload: Dict[str, Any], *,
                          now: Optional[float] = None) -> str:
    """打「需人工」标时的旁路：reason 是 ``adult:explicit|pressure`` 且政策 human → 登记 3 分钟补发。

    只在这一处决定「要不要补发」——drafts 钩子打标带 adult 原因，A 线 / 其它链的打标不带则
    零行为。soft_reply 政策已即时发过（账本有记录）→ 不再补。返回 ``scheduled`` / 跳过原因。
    """
    try:
        return _on_needs_human_tagged(store, cid, reason, payload, now=now)
    except Exception:
        logger.debug("[adult] on_needs_human_tagged 异常（忽略）", exc_info=True)
        return "error"


def _on_needs_human_tagged(store: Any, cid: str, reason: Any, payload: Dict[str, Any], *,
                           now: Optional[float] = None) -> str:
    level, _hit = parse_reason(reason)
    if not level or not is_blocking_level(level):
        return "not_adult"
    if store is None or not cid:
        return "no_store"
    conv = {
        "conversation_id": cid,
        "platform": str((payload or {}).get("platform") or ""),
        "account_id": str((payload or {}).get("account_id") or "default"),
        "chat_key": str((payload or {}).get("chat_key") or ""),
    }
    ts = float(now if now is not None else time.time())
    cfg = None
    try:
        cfg = getattr(_bound_service(), "_cfg", None)
    except Exception:
        cfg = None
    persona = resolve_persona(conv, cfg)
    policy, _src = adult_policy_of(persona, cfg)
    if policy != "human":
        return f"policy_{policy}"
    prev = _kv_get(store, cid)
    try:
        if prev and ts - float(prev.get("ts") or 0) < FOLLOWUP_SEC:
            return "recently_sent"
    except (TypeError, ValueError):
        pass
    lang = str((payload or {}).get("lang") or "")
    ok = schedule_followup(store, conv, level=level, tag_ts=ts, lang=lang, cfg=cfg)
    return "scheduled" if ok else "not_scheduled"


# ── drafts.py 唯一钩子 ───────────────────────────────────────────────────────

def regrade_inbound(svc: Any, conv: Dict[str, Any], text: str, lang: str, risk_level: str,
                    peer_reasons: Sequence[str], risk_hits: Sequence[str], *,
                    automation_mode: str = "auto_ai", cfg: Any = None, persona: Any = None,
                    now: Optional[float] = None,
                    send: bool = True) -> Tuple[str, List[str], Optional[Dict[str, Any]]]:
    """``(risk_level, peer_reasons, info)``。无成人命中 → 原样返回、info=None。

    副作用（explicit / pressure 且政策非 mark_only）：``risk_hold.set(adult)`` + ``tag_needs_human``
    （reason ``adult:<level>:<hit>``）+ soft_reply 政策立即软回应。任何异常 → 原判定放行。
    """
    reasons = [str(r) for r in (peer_reasons or [])]
    try:
        bind_service(svc)
        g = grade(text, lang)
        level = str(g.get("level") or "")
        if not level:
            return risk_level, reasons, None
        cid = str((conv or {}).get("conversation_id") or "")
        store = getattr(svc, "_store", None)
        if cfg is None:
            cfg = getattr(svc, "_cfg", None)
        if persona is None:
            persona = resolve_persona(conv, cfg)
        policy, src = adult_policy_of(persona, cfg)
        hits = [str(h) for h in (g.get("hits") or [])]
        hit0 = hits[0] if hits else ""
        info: Dict[str, Any] = {"level": level, "hits": hits, "pressure_hits": list(g.get("pressure_hits") or []),
                                "policy": policy, "policy_source": src, "category": CATEGORY,
                                "soft_reply": "", "needs_human": False}
        base = [r for r in reasons if r != CATEGORY and not r.startswith(REASON_PREFIX)
                and r not in (FLIRT_REASON, MARK_REASON)]
        _max = _max_risk
        if level in ("mention", "flirt"):
            new_risk = _max(_downgrade_from_adult(risk_level, reasons), "medium")
            out = [FLIRT_REASON] + base + [f"{REASON_PREFIX}{level}"]
            logger.info("[adult] grade conv=%s level=%s hits=%s policy=%s action=pass risk=%s",
                        cid, level, "|".join(hits[:4]) or "-", policy, new_risk)
            return new_risk, out, info
        if level == "explicit" and policy == "mark_only":
            new_risk = _max(_downgrade_from_adult(risk_level, reasons), "medium")
            out = [MARK_REASON] + base + [f"{REASON_PREFIX}{level}"]
            logger.info("[adult] grade conv=%s level=%s hits=%s policy=%s action=mark_only risk=%s",
                        cid, level, "|".join(hits[:4]) or "-", policy, new_risk)
            return new_risk, out, info
        # explicit × (human | soft_reply) / pressure × 任何政策：high + needs_human
        new_risk = _max(risk_level, "high")
        out = [CATEGORY] + base + [f"{REASON_PREFIX}{level}"]
        if hit0:
            out.append(f"adult_hit:{hit0[:40]}")
        ts = float(now if now is not None else time.time())
        tag_reason = f"{REASON_PREFIX}{level}" + (f":{hit0[:40]}" if hit0 else "")
        info["needs_human"] = True
        if store is not None and cid:
            try:
                from src.inbox import risk_hold as _rh
                _rh.set(store, cid, CATEGORY, hits[:3] or level, by="adult_grader", now=ts)
            except Exception:
                logger.debug("[adult] risk_hold.set 失败（忽略）", exc_info=True)
            try:
                from src.integrations.protocol_autoreply import tag_needs_human
                tag_needs_human(store, {"platform": conv.get("platform"), "account_id": conv.get("account_id"),
                                        "chat_key": conv.get("chat_key"), "lang": lang},
                                reason=tag_reason, source="adult_grader", now=ts)
            except Exception:
                logger.debug("[adult] tag_needs_human 失败（忽略）", exc_info=True)
        action = "handoff"
        if policy == "soft_reply" and send:
            res = send_soft_reply(store, conv, level=level, policy=policy, mode="immediate",
                                  persona=persona, lang=lang, cfg=cfg, svc=svc, now=ts, tag_ts=ts,
                                  peer_text=text)
            info["soft_reply"] = str(res.get("text") or "")
            info["soft_reply_status"] = str(res.get("status") or "")
            action = "soft_reply"
        elif policy == "human":
            action = "handoff_followup"   # 3 分钟补发由 tag_needs_human 钩子登记
        logger.info("[adult] grade conv=%s level=%s hits=%s pressure=%s policy=%s(%s) action=%s mode=%s",
                    cid, level, "|".join(hits[:4]) or "-", "|".join(info["pressure_hits"][:3]) or "-",
                    policy, src, action, automation_mode)
        return new_risk, out, info
    except Exception:
        logger.debug("[adult] regrade 异常（原判定放行）", exc_info=True)
        return risk_level, reasons, None


def _downgrade_from_adult(risk_level: str, reasons: Sequence[str]) -> str:
    """原 high 若**只**因 adult 而 high → 回到 low（再由调用方抬到 medium）；有别的高危因子
    （支付词表 keyword / 隐私 / 停联…）则不动。"""
    others = [r for r in (reasons or []) if r != CATEGORY and not str(r).startswith(REASON_PREFIX)]
    if str(risk_level or "low").lower() == "high" and not others:
        return "low"
    return str(risk_level or "low")


def _max_risk(a: str, b: str) -> str:
    try:
        from src.inbox.autosend_policy import max_risk
        return max_risk(a, b)
    except Exception:
        rank = {"low": 0, "medium": 1, "high": 2}
        return a if rank.get(str(a).lower(), 0) >= rank.get(str(b).lower(), 0) else b


def card_label_parts(reason: Any) -> Dict[str, str]:
    """前端 / 诊断共用：``adult:explicit:sex`` → ``{category: adult, level: explicit, hit: sex}``。"""
    level, hit = parse_reason(reason)
    return {"category": CATEGORY if level else "", "level": level, "hit": hit}


__all__ = [
    "CATEGORY", "LEVELS", "POLICIES", "FOLLOWUP_SEC", "KV_PREFIX", "REASON_PREFIX",
    "FLIRT_REASON", "MARK_REASON",
    "grade", "is_blocking_level", "normalize_policy", "default_policy", "adult_policy_of",
    "resolve_persona", "prompt_block", "soft_reply_candidates", "pick_soft_reply", "soft_reply_for",
    "last_soft_reply", "bind_service", "dispatch_soft_reply", "send_soft_reply",
    "run_followup", "schedule_followup", "pending_followups", "cancel_followup",
    "parse_reason", "on_needs_human_tagged", "regrade_inbound", "card_label_parts",
]
