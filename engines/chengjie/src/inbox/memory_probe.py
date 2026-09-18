"""记忆探针（2026-09-18，「还记得我们第一次聊什么吗」→「脑子有点空」事故沉淀）。

事故：会话 204 条 / 60 天，客户问「还记得我们第一次聊什么吗」，最大档窗口里只有断层
修剪后的 3 条旧话 + 情景记忆唯一一条「偏好中文」，LLM 只能装糊涂——而**全部原文都在
inbox 库里**。检索层缺的不是容量，是「按问题取证据」。

本模块：
- :func:`detect_memory_probe`：保守判定客户是否在**考记忆**（第一次聊什么 / 还记得…吗 /
  我叫什么 / 上次我说的…）。词表宁漏勿误——普通聊天绝不触发，零额外 token。
- :func:`extract_probe_keywords`：从问句里抽可检索的实词（去掉探针套话）。
- :func:`select_evidence`：在会话时间线（store 行，ts 升序）里按探针类型取证据：
  ``first_contact`` → 最早几条；``recall`` → 关键词命中行（客户侧优先）+ 前后各一条。
- :func:`build_memory_probe_block`：证据 → 注入块。**硬规则**：只能引用块里的原文；
  块里没有就坦白「记不太清了」，绝不编——编出来的「记忆」是最快的穿帮。
- :func:`probe_from_store`：B 线接线（persona_reply 经 extra_hint 注入）。

全部纯函数 + 一处 store 读；任何异常返回 ""，不影响出稿。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# 探针套话（判定用；同时也是关键词抽取要剔掉的噱头词）
_FIRST_CONTACT_RE = re.compile(
    r"(第一次|最开始|最早|一开始|刚认识|刚开始|初次|最初|first\s+(time|talk|chat|message)|"
    r"when\s+we\s+(first|started))",
    re.IGNORECASE,
)
# 显式「考记忆」标记（单独即可触发）
_EXPLICIT_RE = re.compile(
    r"(还记得|還記得|记不记得|記不記得|记得吗|記得嗎|记得我|記得我|记得不|記得不|"
    r"你忘了|忘了吗|忘了嗎|忘记了吗|忘記了嗎|"
    r"do\s+you\s+remember|(?:still\s+)?remember\s+(what|when|my|that|how|where|who))",
    re.IGNORECASE,
)
# 回指「我之前跟你说过…」——须与问句标记同现才算考记忆（陈述句「我说过我爱吃辣」不触发）
_REF_RE = re.compile(
    r"(我(之前|上次|以前|前几天|前幾天|那天|那次)(跟你|和你|给你|給你|对你|對你)?(说|說|提|讲|講)过?|"
    r"上次(我们|我們|咱们)?(聊|说|說|讲|講)(的|过|過)?)",
    re.IGNORECASE,
)
_QMARK_RE = re.compile(r"[吗嗎?？]|什么|甚麼|啥|哪|呢|多少|几|幾")
_NAME_RE = re.compile(
    r"(我叫什么|我叫啥|我的名字(是什么|叫什么|是啥|呢)|叫我什么|叫我啥|"
    r"what('s|\s+is)\s+my\s+name|know\s+my\s+name)",
    re.IGNORECASE,
)

_STOP_TOKENS = (
    "还记得", "還記得", "记不记得", "記不記得", "记得", "記得", "忘了", "第一次", "最开始",
    "一开始", "最早", "刚认识", "刚开始", "我们", "我們", "你我", "咱们", "上次", "之前",
    "以前", "那天", "前几天", "说过", "說過", "提过", "提過", "讲过", "講過", "跟你", "和你",
    "给你", "給你", "对你", "什么", "甚麼", "啥", "吗", "嗎", "呢", "吧", "啊", "呀", "的",
    "了", "是", "有", "在", "我", "你", "他", "她", "它", "聊", "说", "說", "讲", "講", "提",
    "过", "過", "不", "没", "沒", "还", "還", "就", "都", "也", "很", "到", "和", "与", "跟",
    "那个", "这个", "那件", "这件", "事情", "事", "东西", "时候", "時候", "一下", "一次",
    "记", "記", "得",
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
_LATIN_STOP = frozenset({
    "you", "remember", "what", "when", "the", "that", "this", "did", "was", "were", "have",
    "had", "our", "first", "time", "talk", "chat", "said", "told", "about", "and", "with",
    "for", "are", "your", "name", "still", "know", "think", "we", "me", "my",
})

MAX_EVIDENCE = 6
QUOTE_CHARS = 90


def detect_memory_probe(text: str) -> Optional[str]:
    """客户在考记忆？→ ``first_contact`` / ``name`` / ``recall``；否则 None。纯函数。"""
    t = str(text or "").strip()
    if not t or len(t) > 200:
        return None
    explicit = bool(_EXPLICIT_RE.search(t))
    q = bool(_QMARK_RE.search(t))
    if _NAME_RE.search(t) or (explicit and re.search(r"名字|叫我|my name", t, re.I)):
        return "name"
    if _FIRST_CONTACT_RE.search(t) and (
        explicit or (q and re.search(r"聊|说|說|讲|講|talk|chat|said|message", t, re.I))
    ):
        return "first_contact"
    if explicit or (_REF_RE.search(t) and q):
        return "recall"
    return None


#: 多字套话才从原文抹掉；单字（的/了/到/是）只在抽完后当过滤，绝不 replace——
#: 「吃到撑」被切成「吃 撑」后关键词清空，是观测用例踩到的漏召回。
_STOP_MULTI = tuple(s for s in _STOP_TOKENS if len(s) >= 2)
_STOP_SET = frozenset(_STOP_TOKENS)
_STOP_SINGLE = frozenset(s for s in _STOP_TOKENS if len(s) == 1)
_TRAIL_PARTICLES = frozenset("吗嗎呢吧啊呀")

#: 轻量同义（不引入嵌入服务）：关键词对不上原文时补一组常见说法。
#: 嵌入未配置（173 常 skip_unready）时这是「涮肉→火锅」的唯一兜底。
_SYNONYM_GROUPS = (
    frozenset({"火锅", "火鍋", "涮肉", "麻辣烫", "麻辣燙", "hotpot"}),
    frozenset({"照片", "相片", "图片", "圖片", "自拍", "photo", "picture", "selfie"}),
    frozenset({"语音", "語音", "voice", "audio"}),
    frozenset({"名字", "姓名", "name"}),
)


def extract_probe_keywords(text: str) -> List[str]:
    """问句 → 可检索实词（CJK 2 字滑窗 + 拉丁词），去探针套话；最多 8 个。"""
    t = str(text or "")
    for s in _STOP_MULTI:
        t = t.replace(s, " ")
    out: List[str] = []
    for run in _CJK_RE.findall(t):
        run = _clean_cjk_run(run)
        if len(run) >= 2:
            if len(run) <= 4:
                out.append(run)
            else:
                # 长串按 2 字滑窗（无分词依赖；命中即算）
                out.extend(run[i:i + 2] for i in range(0, len(run) - 1))
    for w in _LATIN_RE.findall(t):
        wl = w.lower()
        if wl not in _LATIN_STOP:
            out.append(wl)
    seen = set()
    uniq = []
    for k in out:
        if k in _STOP_SET or k.lower() in _LATIN_STOP:
            continue
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq[:8]


def _clean_cjk_run(run: str) -> str:
    """剥掉粘在片段两端的单字虚词 / 语气词（「面试吗」→「面试」，「说吃到撑」→「吃到撑」）。"""
    s = str(run or "")
    while s and s[0] in _STOP_SINGLE:
        s = s[1:]
    while s and (s[-1] in _TRAIL_PARTICLES or s[-1] in _STOP_SINGLE):
        s = s[:-1]
    return s


def expand_probe_keywords(keywords: List[str]) -> List[str]:
    """关键词 ∪ 同义组（子串互含也算，如「涮肉店」含「涮肉」）。纯函数。"""
    extra: List[str] = []
    seen = {str(k).lower() for k in (keywords or [])}
    for k in keywords or []:
        if not k:
            continue
        kl = k.lower()
        for g in _SYNONYM_GROUPS:
            if k in g or kl in g or any(
                (len(m) >= 2 and (m in k or k in m or m.lower() in kl or kl in m.lower()))
                for m in g
            ):
                for alt in g:
                    al = alt.lower()
                    if al not in seen:
                        seen.add(al)
                        extra.append(alt)
    return list(keywords or []) + extra


def _row_text(r: Dict[str, Any]) -> str:
    return str(r.get("text") or r.get("content") or "").strip()


def _row_is_user(r: Dict[str, Any]) -> bool:
    role = str(r.get("role") or "")
    if role:
        return role == "user"
    return str(r.get("direction") or "") in ("in", "inbound")


def _row_ts(r: Dict[str, Any]) -> float:
    try:
        return float(r.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _when(ts: float, now: float) -> str:
    if ts <= 0:
        return "时间不详"
    try:
        d = datetime.fromtimestamp(ts)
        date = f"{d.month}月{d.day}日"
    except Exception:
        date = "时间不详"
    try:
        from src.inbox.time_context import describe_age
        rel = describe_age(max(0.0, now - ts))
    except Exception:
        rel = ""
    return f"{date}，约{rel}前" if rel and rel != "不到 1 小时" else date


def select_evidence(
    rows: List[Dict[str, Any]],
    kind: str,
    keywords: List[str],
    *,
    current_text: str = "",
    max_items: int = MAX_EVIDENCE,
) -> List[Dict[str, Any]]:
    """时间线（ts 升序）→ 证据行（ts 升序）。跳过本条问句本身与系统占位行。"""
    items = [r for r in (rows or []) if isinstance(r, dict) and _row_text(r)]
    cur = str(current_text or "").strip()
    if cur:
        # 去掉末尾的本条问句（它不是证据）
        for i in range(len(items) - 1, max(-1, len(items) - 4), -1):
            if _row_is_user(items[i]) and _row_text(items[i]) == cur:
                items.pop(i)
                break
    items = [r for r in items if not _row_text(r).startswith("[")]
    if not items:
        return []
    if kind == "first_contact":
        return items[:max_items]
    if kind == "name":
        hits = [r for r in items if _row_is_user(r)
                and re.search(r"我叫|我是|叫我|名字|my name is|call me|i am|i'm", _row_text(r), re.I)]
        return hits[:max_items]
    kws = expand_probe_keywords([k for k in (keywords or []) if k])
    if not kws:
        return []
    scored: List[Tuple[int, int]] = []
    for idx, r in enumerate(items):
        txt = _row_text(r).lower()
        score = sum(1 for k in kws if k.lower() in txt)
        if score:
            score += 1 if _row_is_user(r) else 0      # 客户亲口说的优先
            scored.append((score, idx))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = set()
    for _, idx in scored[:max_items]:
        picked.add(idx)
        # 前后各带一条，让引用有语境
        if idx - 1 >= 0:
            picked.add(idx - 1)
        if idx + 1 < len(items):
            picked.add(idx + 1)
    out = [items[i] for i in sorted(picked)]
    return out[: max_items + 4]


def build_memory_probe_block(
    kind: str,
    evidence: List[Dict[str, Any]],
    *,
    now: Optional[float] = None,
    total_msgs: int = 0,
    first_ts: float = 0.0,
) -> str:
    """证据 → 注入块。无证据也返回块（坦白规则），调用方可据 kind 决定是否注入。"""
    now_ts = float(now or time.time())
    head = {
        "first_contact": "对方在问你们**最初/第一次**聊了什么。",
        "name": "对方在考你记不记得 TA 的名字/怎么称呼。",
        "recall": "对方在考你记不记得 TA 之前说过的某件事。",
    }.get(kind, "对方在考你的记忆。")
    lines = [f"【记忆检索——重要】{head}"]
    if first_ts > 0 or total_msgs > 0:
        meta = []
        if first_ts > 0:
            meta.append(f"你们最早的对话在 {_when(first_ts, now_ts)}")
        if total_msgs > 0:
            meta.append(f"至今共约 {total_msgs} 条消息")
        lines.append("；".join(meta) + "。")
    if evidence:
        lines.append("以下是从聊天记录里查到的**原文**（只可引用这些，不可添加记录里没有的细节）：")
        for r in evidence:
            who = "对方" if _row_is_user(r) else "你"
            txt = _row_text(r).replace("\n", " ")
            if len(txt) > QUOTE_CHARS:
                txt = txt[: QUOTE_CHARS - 1] + "…"
            lines.append(f"- {_when(_row_ts(r), now_ts)} {who}：「{txt}」")
        lines.append(
            "回答要像真人回忆：挑 1-2 个最有画面感的点自然带出（「那会儿你还…」），"
            "说时间用大概（「一个多月前」），不要念日期和条数，不要罗列全部。"
        )
    else:
        lines.append(
            "聊天记录里**没有**能对上的内容。坦白说记不太清了（「你这么一问我还真有点想不起来，"
            "你提醒我一下？」），把话头交回对方——**绝不能编**一个「记忆」凑数，编错了是最伤信任的穿帮。"
        )
    return "\n".join(lines)


def probe_from_store(
    store: Any,
    conversation_id: str,
    text: str,
    *,
    scan_limit: int = 500,
    now: Optional[float] = None,
) -> str:
    """B 线接线：探针判定 → 读 store 时间线 → 证据块；未触发 / 异常 → ""。"""
    block, _kind, _n = probe_detail_from_store(
        store, conversation_id, text, scan_limit=scan_limit, now=now)
    return block


def probe_detail_from_store(
    store: Any,
    conversation_id: str,
    text: str,
    *,
    scan_limit: int = 500,
    now: Optional[float] = None,
    semantic_rows: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str, int]:
    """:func:`probe_from_store` 的带观测版 → ``(注入块, 探针类型, 证据条数)``。

    未触发 / 异常 → ``("", "", 0)``。``semantic_rows``（2026-09-18 语义召回）：调用方用
    向量召回补来的行（已按 ts 升序），在关键词证据不足时合并进证据（去重、保序）——
    子串匹配对同义改写（「火锅」→「涮肉」）无能为力，这是它的兜底。
    """
    kind = detect_memory_probe(text)
    if not kind or store is None or not str(conversation_id or "").strip():
        return "", "", 0
    try:
        cid = str(conversation_id).strip()
        rows: List[Dict[str, Any]] = []
        if kind == "first_contact" and hasattr(store, "list_messages"):
            rows = list(store.list_messages(cid, limit=MAX_EVIDENCE + 4) or [])
        else:
            rows = load_probe_rows(store, cid, scan_limit=scan_limit)
        total = 0
        first_ts = 0.0
        try:
            total = int(store.count_messages(cid) or 0)
        except Exception:
            total = len(rows)
        try:
            oldest = store.list_messages(cid, limit=1) if hasattr(store, "list_messages") else []
            if oldest:
                first_ts = _row_ts(oldest[0])
        except Exception:
            first_ts = _row_ts(rows[0]) if rows else 0.0
        kws = extract_probe_keywords(text) if kind == "recall" else []
        if kind == "recall" and not kws and not semantic_rows:
            # 「还记得吗」没有任何实词 → 不知道对方指什么，让 LLM 反问而不是猜
            return (build_memory_probe_block(kind, [], now=now, total_msgs=total, first_ts=first_ts),
                    kind, 0)
        ev = select_evidence(rows, kind, kws, current_text=text) if kws or kind != "recall" else []
        if semantic_rows and kind == "recall":
            ev = merge_evidence(ev, semantic_rows, current_text=text)
        return (build_memory_probe_block(kind, ev, now=now, total_msgs=total, first_ts=first_ts),
                kind, len(ev))
    except Exception:
        return "", "", 0


def load_probe_rows(store: Any, conversation_id: str, *, scan_limit: int = 500) -> List[Dict[str, Any]]:
    """读会话时间线（ts 升序、不含软删）供证据筛选 / 语义索引共用；异常 → []。"""
    cid = str(conversation_id or "").strip()
    if store is None or not cid:
        return []
    try:
        try:
            return list(store.list_recent_messages(
                cid, limit=int(scan_limit), include_deleted=False) or [])
        except TypeError:
            return list(store.list_recent_messages(cid, limit=int(scan_limit)) or [])
    except Exception:
        return []


def merge_evidence(
    keyword_rows: List[Dict[str, Any]],
    semantic_rows: List[Dict[str, Any]],
    *,
    current_text: str = "",
    max_items: int = MAX_EVIDENCE + 4,
) -> List[Dict[str, Any]]:
    """关键词证据 ∪ 语义证据 → 去重（message_id / (ts,text)）、剔当前问题、按 ts 升序、截断。纯函数。"""
    cur = str(current_text or "").strip()
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in list(keyword_rows or []) + list(semantic_rows or []):
        if not isinstance(r, dict):
            continue
        txt = _row_text(r)
        if not txt or txt.startswith("["):
            continue
        if cur and _row_is_user(r) and txt == cur:
            continue
        key = str(r.get("message_id") or "") or f"{_row_ts(r)}|{txt[:60]}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    out.sort(key=_row_ts)
    return out[:max_items]


__all__ = [
    "detect_memory_probe", "extract_probe_keywords", "expand_probe_keywords", "select_evidence",
    "build_memory_probe_block", "probe_from_store", "probe_detail_from_store",
    "load_probe_rows", "merge_evidence",
]
