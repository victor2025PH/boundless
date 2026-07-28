"""每人设相册「按关键词触发挑媒体」匹配器（纯函数，可单测/离线）。

给「运营配触发词 → 对话命中就发对应图/视频」提供选择逻辑。**只做挑选，不碰 IO**
（取行由 ``persona_media_store`` 负责；发送由 image_autosend / skill_manager 负责）。

匹配语义（简单、可解释、零误伤）：
- 条目带 ``triggers``（关键词列表）：客户文本包含任一关键词 → **精确命中**（specific）。
- 条目 ``triggers`` 为空 = **通用相册池**：仅当调用方判定这是一次「泛化要照片/自拍」请求
  （``generic_ok=True``）时才作候选——对应老「随机挑一张自拍」的行为，向后兼容。
- **优先级**：只要有精确命中就用精确命中池，否则才回落通用池；都空 → None（交生成/文字兜底）。
- 多候选：``weight`` 加权随机 + **尽量避开上一条**（``avoid_id``）防连发重复（新鲜感）。
- 可选按 ``media_types`` 过滤（只要图/只要视频）与 ``bond_level`` 关系闸门（默认不设=不限）。

图文一致性语义（P0，2026-07-27——「发的图和说的话/时间对不上」事故收口）：
- **场景类**（``scene_class_of``）：生图场景短语（英）/客户点名场景/策展文件名前缀
  归一到同一套 canonical 场景类；客户显式点名场景时（``required_scene_class``），
  通用池只出场景匹配的条目——匹配不到宁可交生成/诚实文字，绝不拿随机人像顶包。
- **时段**（``tod:day|night`` 标签 + ``tod_conflicts_with_hour``）：白天照深夜不发、
  夜景照大白天不发（**软过滤**：全冲突时放行但调用方须按「之前拍的」口径配文）。
- **重发冷却**（``hard_exclude_ids``）：同一张图对同一会话 N 小时内绝不重发
  （原「翻旧照」层几分钟内就能重发同图——真人不会 2 分钟发同一张照片两次）。
- **服装连续性**（``prefer_series``）：同会话短窗口内连拍优先同系列（同套衣服）
  ——真人「再拍一张」不会 30 秒换一身衣服。
"""
from __future__ import annotations

import random
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

POOL_KEYWORD = "keyword"
POOL_GENERIC = "generic"
POOL_NONE = "none"

# ── 场景类词表（保守：认不出 → ""=场景未知，由调用方决定放行/拒绝）──────────
# 覆盖三类输入：生图英文短语（_REQUESTED_SCENE_MAP/scene_rotation 产物）、
# 中文点名词、策展文件名 slug（car / home-sofa / outdoor-park / mirror-selfie…）。
_SCENE_CLASS_WORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("beach", ("beach", "seaside", "seascape", "ocean", "海边", "海邊", "沙滩", "沙灘")),
    ("cafe", ("cafe", "coffee", "咖啡")),
    ("office", ("office", "workday", "办公室", "辦公室", "上班")),
    ("gym", ("gym", "workout", "sporty outfit", "健身")),
    ("kitchen", ("kitchen", "cooking", "apron", "厨房", "廚房", "做饭", "做菜")),
    ("park", ("park", "outdoor", "natural daylight", "公园", "公園", "户外", "戶外")),
    ("bedroom", ("bedroom", "卧室", "臥室", "床上")),
    ("library", ("library", "bookstore", "bookshel", "图书馆", "圖書館", "书店", "書店")),
    ("street", ("street", "shopping", "night market", "逛街", "夜市", "街头", "街頭")),
    ("campus", ("campus", "classroom", "校园", "校園", "教室")),
    ("restaurant", ("restaurant", "dinner table", "餐厅", "餐廳")),
    ("night_city", ("night lights", "night view", "city night", "夜景")),
    # home 放后：短语常同时含 "cozy home"+"couch"，先匹配更具体的类
    ("home", ("at home", "home interior", "cozy home", "couch", "sofa", "living room",
              "mirror selfie", "mirror-selfie", "家里", "家裡", "在家", "沙发", "沙發")),
)
# "car" 单词太短（cardigan/carpet 会误命中），用词边界正则单独判。
_CAR_RE = re.compile(r"(?:\bcar\b|车里|車裡|车内|車內|车上|車上|开车|開車|驾驶|駕駛)")


def scene_class_of(phrase: Any) -> str:
    """把场景短语/文件名 slug 归一到 canonical 场景类；认不出返回 ""（场景未知）。"""
    s = str(phrase or "").strip().lower().replace("_", " ").replace("-", " ")
    if not s:
        return ""
    for cls, words in _SCENE_CLASS_WORDS:
        for w in words:
            if w in s:
                return cls
    if _CAR_RE.search(s):
        return "car"
    return ""


def tod_conflicts_with_hour(tod: str, hour: Any) -> bool:
    """照片时段标注与当前小时是否硬冲突（纯函数）。

    ``tod``：``day``（画面明显白天）/``night``（画面明显夜晚）/其它或空=不限。
    深夜 22-6 点不发白天照、白天 6-17 点不发夜景照；17-22 傍晚两者都放
    （黄昏画面歧义大，宁可放过不误杀）。
    """
    t = str(tod or "").strip().lower()
    if t not in ("day", "night"):
        return False
    try:
        h = int(hour)
    except (TypeError, ValueError):
        return False
    if t == "day":
        return h >= 22 or h < 6
    return 6 <= h < 17


def _tag_value(row: Optional[Dict[str, Any]], prefix: str) -> str:
    for t in (row or {}).get("tags") or []:
        ts = str(t or "")
        if ts.startswith(prefix):
            return ts[len(prefix):].strip()
    return ""


def row_tod(row: Optional[Dict[str, Any]]) -> str:
    """条目的时段标注（tags 里 ``tod:day|night``）；无则空串=不限。"""
    return _tag_value(row, "tod:").lower()


def row_scene_class(row: Optional[Dict[str, Any]]) -> str:
    """条目的场景类：``tags scene:<短语>`` 归一 → 回落策展文件名前缀解析；无则 ""。"""
    cls = scene_class_of(_tag_value(row, "scene:"))
    if cls:
        return cls
    # 策展命名 <scene>_<series>_<nn>.jpg → scene 段
    fp = str((row or {}).get("file_path") or (row or {}).get("url") or "")
    if fp:
        stem = fp.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        parts = stem.split("_")
        if len(parts) >= 3 and parts[-1].isdigit():
            return scene_class_of(parts[0])
    return ""


def normalize_text(text: Any) -> str:
    return str(text or "").strip().lower()


# ── 信息性提问守卫（2026-07-27 实锤）───────────────────────────────────────
# 「你喝什么咖啡」「你是跑了几家咖啡馆」含触发词"咖啡"被关键词池连发多张图、
# 正文从未正面作答，被测试号骂「已读乱回」；「这是哪里拍的呢」追问上一张也被
# 当成要图再发一张。疑问句而且**没有求图动词** → 客户在问名词本身，不是在要
# 媒体——关键词池让路，交正常 LLM 答话（通用池本就要 detect_selfie_request
# 才开，不受此影响）。宁少发勿乱发：陈述句里带触发词照常命中。
_QUESTION_RE = re.compile(
    r"[？?]|什么|什麼|甚麼|啥|哪(?:里|裡|儿|兒|个|個|家|种|種)?|几[家个條条张張次样樣]|幾|"
    r"怎么|怎麼|怎样|怎樣|为什么|為什麼|为啥|為啥|多少|"
    r"[吗嗎]|[呢嘛么麼]\s*$|"
    r"\b(?:what|which|where|when|why|how|who)\b"
)
_MEDIA_REQ_RE = re.compile(
    r"看看|看一?下|给我看|給我看|让我看|讓我看|想看|求(?:图|圖|照)|"
    r"[来來]一?[张張个個条條]|再[来來发發拍]|"
    r"[发發](?:一?[张張个個]|[张張]|我|[来來])|"
    r"拍一?[张張个個]|"
    r"有[没沒]有.{0,8}(?:照片|相片|图|圖|视频|視頻|影片|自拍)|"
    r"(?:照片|相片|图片|圖片|视频|視頻|影片|自拍).{0,4}(?:有[吗嗎]|有[没沒])|"
    r"show\s+me|send\s+(?:me\s+)?(?:a\s+)?(?:pic|photo|video|selfie)|"
    r"can\s+i\s+see|got\s+any",
    re.IGNORECASE)


def is_info_question(text: Any) -> bool:
    """客户消息是「信息性提问」（问名词本身而非要图）→ True＝关键词池应让路。"""
    t = normalize_text(text)
    if not t:
        return False
    if not _QUESTION_RE.search(t):
        return False
    return not _MEDIA_REQ_RE.search(t)


def _triggers_hit(triggers: Sequence[Any], text_norm: str) -> bool:
    for t in triggers or []:
        ts = str(t or "").strip().lower()
        if ts and ts in text_norm:
            return True
    return False


def _partition(
    rows: Sequence[Dict[str, Any]], text: str, *,
    media_types: Optional[Sequence[str]] = None,
    bond_level: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把候选行分成 (关键词命中, 通用池)，同时过 enabled / 类型 / 关系闸门。"""
    tn = normalize_text(text)
    mt_set = {str(m).strip().lower() for m in media_types} if media_types else None
    keyword: List[Dict[str, Any]] = []
    generic: List[Dict[str, Any]] = []
    for r in rows or []:
        if not r.get("enabled", True):
            continue
        if mt_set and str(r.get("media_type") or "").lower() not in mt_set:
            continue
        if bond_level is not None and int(r.get("min_bond_level") or 0) > int(bond_level):
            continue
        trg = r.get("triggers") or []
        if trg:
            if _triggers_hit(trg, tn):
                keyword.append(r)
        else:
            generic.append(r)
    return keyword, generic


def _weighted_choice(
    rows: Sequence[Dict[str, Any]], rng: Optional[random.Random] = None,
) -> Dict[str, Any]:
    r = rng or random
    weights = [max(1, int(x.get("weight") or 1)) for x in rows]
    return r.choices(list(rows), weights=weights, k=1)[0]


def series_of(row: Optional[Dict[str, Any]]) -> str:
    """条目的系列标签（tags 里 ``series:<xxx>``）；无则空串。

    系列=同套服装/同场景连拍的视觉近似组（album_curator 按 outfit 聚类打标）。
    """
    for t in (row or {}).get("tags") or []:
        ts = str(t or "")
        if ts.startswith("series:"):
            return ts[7:]
    return ""


def _fold_file_key(v: Any) -> str:
    """文件名归一（取 basename + casefold）——比对用，不用于落库。"""
    s = str(v or "").strip().replace("\\", "/")
    if not s:
        return ""
    return s.rsplit("/", 1)[-1].casefold()


def row_file_key(row: Optional[Dict[str, Any]]) -> str:
    """条目的**跨链身份**：文件名（casefold）。无 file_path/url → ""。

    注册相册链按 DB uuid 记账、文件系统相册链按文件名记账；两条链要认出
    「同一张图刚发过」只能靠文件名（2026-07-28 重复发图事故的收口点）。
    """
    r = row or {}
    return _fold_file_key(r.get("file_path") or r.get("url") or "")


def select_media(
    rows: Sequence[Dict[str, Any]], text: str, *,
    generic_ok: bool = False,
    media_types: Optional[Sequence[str]] = None,
    avoid_id: str = "",
    bond_level: Optional[int] = None,
    rng: Optional[random.Random] = None,
    exclude_ids: Optional[Any] = None,
    exclude_series: Optional[Any] = None,
    now_hour: Optional[int] = None,
    required_scene_class: str = "",
    hard_exclude_ids: Optional[Any] = None,
    hard_exclude_files: Optional[Any] = None,
    prefer_series: str = "",
) -> Optional[Dict[str, Any]]:
    """从候选行里挑一个媒体条目；挑不到返回 None。纯函数（rng 可注入以确定性测试）。

    防复读分层回落（2026-07-22）：``exclude_ids``＝该会话已发过的条目、
    ``exclude_series``＝已发过的系列（同套服装连拍）。优先挑「没发过且系列也新」
    的；全排除后**逐层放宽**：先允许旧系列的新图，再允许重发（此时挑
    ``last_sent_at`` 最老的——像真人"翻旧照"，永不因记忆而拒发）。

    图文一致性（P0，2026-07-27）：
    - ``hard_exclude_ids``：重发冷却内的条目，**任何层都不放宽**（几分钟内重发
      同图=机器人实锤）；全被冷却排除 → None（交生成/诚实文字）。
    - ``hard_exclude_files``：同上，但按**文件名**排除——文件系统相册链按文件名
      记账，只查 id 会漏掉「同一张图刚由另一条链发出去过」（2026-07-28 实录）。
    - ``now_hour``：软时段过滤——剔掉 ``tod:`` 标注与当前小时硬冲突的条目；
      剔空则放行原池（配文层须按「之前拍的」口径兜底诚实）。
    - ``required_scene_class``：客户显式点名场景时，**通用池**只出场景类匹配的
      条目（场景未知=不匹配），匹配不到 → None；关键词命中池不受限
      （运营显式绑定的触发词优先级最高）。
    - ``prefer_series``：连续性窗口内优先同系列未发条目（同套衣服再拍一张）。
    """
    keyword, generic = _partition(
        rows, text, media_types=media_types, bond_level=bond_level)
    # 信息性提问不劫持：疑问句且无求图动词 → 关键词池让路（见 is_info_question）。
    if keyword and is_info_question(text):
        keyword = []
    pool = keyword if keyword else (generic if generic_ok else [])
    from_generic = not keyword
    if not pool:
        return None
    # 重发冷却：硬排除，绝不逐层放宽（id 面 + 文件名面，两条链同一张图都算）。
    hx = {str(x) for x in (hard_exclude_ids or ())}
    if hx:
        pool = [r for r in pool if str(r.get("id")) not in hx]
        if not pool:
            return None
    hxf = {_fold_file_key(x) for x in (hard_exclude_files or ())}
    hxf.discard("")
    if hxf:
        pool = [r for r in pool if row_file_key(r) not in hxf]
        if not pool:
            return None
    # 场景硬要求（仅通用池）：点名要海边就只出海边，挑不到交回生成/文字。
    if required_scene_class and from_generic:
        pool = [r for r in pool if row_scene_class(r) == required_scene_class]
        if not pool:
            return None
    # 时段软过滤：白天照深夜不发（有标注才判；剔空放行——配文层兜底诚实）。
    if now_hour is not None:
        lit = [r for r in pool if not tod_conflicts_with_hour(row_tod(r), now_hour)]
        if lit:
            pool = lit
    if avoid_id:
        alt = [r for r in pool if str(r.get("id")) != str(avoid_id)]
        if alt:
            pool = alt
    # 服装连续性：短窗口内「再拍一张」优先同系列（未发过的那几张）。
    if prefer_series:
        hx_all = hx | {str(x) for x in (exclude_ids or ())}
        same = [r for r in pool
                if series_of(r) == prefer_series
                and str(r.get("id")) not in hx_all]
        if same:
            return _weighted_choice(same, rng)
    ex_ids = {str(x) for x in (exclude_ids or ())}
    ex_series = {str(x) for x in (exclude_series or ()) if str(x)}
    if ex_ids or ex_series:
        # 层1：id 新 + 系列新
        fresh = [r for r in pool
                 if str(r.get("id")) not in ex_ids
                 and (series_of(r) not in ex_series or not series_of(r))]
        if fresh:
            return _weighted_choice(fresh, rng)
        # 层2：id 新（允许旧系列——同系列另一张，好过重发同一张）
        unsent = [r for r in pool if str(r.get("id")) not in ex_ids]
        if unsent:
            return _weighted_choice(unsent, rng)
        # 层3：全发过 → 挑最久没发的（翻旧照），不拒发
        return min(pool, key=lambda r: float(r.get("last_sent_at") or 0))
    return _weighted_choice(pool, rng)


def explain_match(
    rows: Sequence[Dict[str, Any]], text: str, *,
    generic_ok: bool = True,
    media_types: Optional[Sequence[str]] = None,
    bond_level: Optional[int] = None,
) -> Dict[str, Any]:
    """「试触发」用：返回这句话会命中哪个池 + 全部候选（不做加权随机，列全供预览）。"""
    keyword, generic = _partition(
        rows, text, media_types=media_types, bond_level=bond_level)
    info_q = bool(keyword) and is_info_question(text)
    if info_q:
        keyword = []  # 与 select_media 同口径：预览不骗人
    if keyword:
        pool, cands = POOL_KEYWORD, keyword
    elif generic_ok and generic:
        pool, cands = POOL_GENERIC, generic
    else:
        pool, cands = POOL_NONE, []
    return {
        "pool": pool,
        "info_question": info_q,
        "candidates": [
            {"id": c.get("id"), "media_type": c.get("media_type"),
             "url": c.get("url"), "caption": c.get("caption"),
             "triggers": c.get("triggers") or [], "weight": c.get("weight")}
            for c in cands
        ],
        "keyword_count": len(keyword),
        "generic_count": len(generic),
    }


def pick_media(
    store: Any, persona_id: str, text: str, *,
    generic_ok: bool = False,
    media_types: Optional[Sequence[str]] = None,
    avoid_id: str = "",
    bond_level: Optional[int] = None,
    rng: Optional[random.Random] = None,
    conv_key: str = "",
    resend_after_days: float = 90,
    now_hour: Optional[int] = None,
    required_scene_class: str = "",
    resend_cooldown_hours: float = 0,
    continuity_minutes: float = 0,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """store 便捷封装：取该人设 enabled 行 → select_media。store/persona 缺失 → None。

    ``conv_key`` 非空时启用防复读记忆：查该会话已发账本（id+系列）作排除面。
    ``resend_after_days``：账本时间衰减（默认 90 天后旧图重新可用；0=永久排除）。
    ``resend_cooldown_hours``（P0 一致性）：冷却窗内发过的条目硬排除（任何回落层
    都不重发）；``continuity_minutes``：最近一次发图在窗口内 → 优先同系列
    （同套衣服连拍），防「30 秒换一身衣服」穿帮。均需 ``conv_key``。
    """
    if store is None or not persona_id:
        return None
    try:
        rows = store.list(str(persona_id), enabled_only=True)
    except Exception:
        return None
    ex_ids, ex_series = None, None
    hard_ids: Optional[set] = None
    hard_files: Optional[set] = None
    prefer_series = ""
    if conv_key:
        try:
            import time as _time
            _now = float(now if now is not None else _time.time())
            # now 须同步透传账本的时间衰减过滤——冷却/连续性与排除面用不同时钟
            # 会口径分裂（注入 now 测试时账本按墙钟衰减，冷却却按注入值判）。
            hist = store.sent_history(
                str(conv_key), max_age_days=float(resend_after_days or 0),
                now=_now)
            ex_ids = hist.get("ids") or None
            ex_series = hist.get("series") or None
            items = hist.get("items") or []
            if resend_cooldown_hours and resend_cooldown_hours > 0:
                cutoff = _now - float(resend_cooldown_hours) * 3600.0
                _fresh = [it for it in items
                          if float(it.get("ts") or 0) >= cutoff]
                hard_ids = {str(it.get("id")) for it in _fresh} or None
                # 跨链：另一条链按文件名记的账，只查 id 会漏
                hard_files = {str(it.get("file_key") or "")
                              for it in _fresh} - {""} or None
            if continuity_minutes and continuity_minutes > 0 and items:
                latest = max(items, key=lambda it: float(it.get("ts") or 0))
                if float(latest.get("ts") or 0) >= _now - float(
                        continuity_minutes) * 60.0:
                    prefer_series = str(latest.get("series") or "")
        except Exception:
            ex_ids = ex_series = None
            hard_ids = hard_files = None
            prefer_series = ""
    return select_media(
        rows, text, generic_ok=generic_ok, media_types=media_types,
        avoid_id=avoid_id, bond_level=bond_level, rng=rng,
        exclude_ids=ex_ids, exclude_series=ex_series,
        now_hour=now_hour, required_scene_class=required_scene_class,
        hard_exclude_ids=hard_ids, hard_exclude_files=hard_files,
        prefer_series=prefer_series)


def caption_for(row: Optional[Dict[str, Any]], lang: str = "", *, fallback: str = "") -> str:
    """取条目配文：优先 ``caption_i18n[lang]`` → ``caption`` → fallback。"""
    row = row or {}
    ci = row.get("caption_i18n") or {}
    if lang and isinstance(ci, dict):
        c = str(ci.get(lang) or "").strip()
        if c:
            return c
    return str(row.get("caption") or "").strip() or fallback


__all__ = [
    "POOL_KEYWORD", "POOL_GENERIC", "POOL_NONE",
    "normalize_text", "select_media", "explain_match", "pick_media", "caption_for",
    "scene_class_of", "tod_conflicts_with_hour", "row_tod", "row_scene_class",
    "row_file_key", "series_of", "is_info_question",
]
