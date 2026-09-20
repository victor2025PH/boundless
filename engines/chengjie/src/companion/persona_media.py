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
    ("park", ("park", "outdoor", "natural daylight", "公园", "公園", "户外", "戶外",
              "风景", "風景", "landscape", "scenery", "scenic")),
    ("bedroom", ("bedroom", "卧室", "臥室", "床上")),
    ("library", ("library", "bookstore", "bookshel", "图书馆", "圖書館", "书店", "書店")),
    # 实施69：烧烤/烤串/大排档并入 street（哈尔滨烧烤店老板娘人设的职业场景
    # 此前词表全盲——人设媒体缺口体检与客户点名「发张烤串的」都定向不了）。
    ("street", ("street", "shopping", "night market", "逛街", "夜市", "街头",
                "街頭", "烧烤", "燒烤", "烤串", "撸串", "擼串", "大排档",
                "大排檔", "路边摊", "路邊攤", "街边摊", "街邊攤")),
    ("campus", ("campus", "classroom", "校园", "校園", "教室")),
    ("restaurant", ("restaurant", "dinner table", "餐厅", "餐廳")),
    ("night_city", ("night lights", "night view", "city night", "夜景")),
    # home 放后：短语常同时含 "cozy home"+"couch"，先匹配更具体的类
    ("home", ("at home", "home interior", "cozy home", "couch", "sofa", "living room",
              "mirror selfie", "mirror-selfie", "家里", "家裡", "在家", "沙发", "沙發")),
)
# "car" 单词太短（cardigan/carpet 会误命中），用词边界正则单独判。
_CAR_RE = re.compile(r"(?:\bcar\b|车里|車裡|车内|車內|车上|車上|开车|開車|驾驶|駕駛)")

# 全部 canonical 场景类（词表 14 类 + car）——上传自动打标的 prompt 枚举与
# 归一化（media_taxonomy.normalize_scene）共用这一份，别在别处手抄。
SCENE_CLASSES: Tuple[str, ...] = tuple(c for c, _ in _SCENE_CLASS_WORDS) + ("car",)

# Q-6 E：产品场景 kind（自拍/室内/室外风景/美食/宠物/其他）——A 段清单与「索风景不再抓自拍」。
SCENE_KINDS: Tuple[str, ...] = ("selfie", "indoor", "outdoor", "food", "pet", "other")
_OUTDOOR_CLASSES = {"beach", "park", "street", "night_city"}
_FOOD_CLASSES = {"kitchen", "restaurant"}
_INDOOR_CLASSES = {
    "cafe", "office", "gym", "kitchen", "bedroom", "library", "campus",
    "restaurant", "home", "car",
}
_SELFIE_CLASSES = {"home", "bedroom"}
_FOOD_OBJ = (
    "food", "meal", "dish", "cake", "coffee", "noodle", "sushi", "pizza",
    "dessert", "早餐", "午餐", "晚餐", "蛋糕", "咖啡", "火锅", "麵", "面",
)
_PET_OBJ = ("dog", "cat", "pet", "puppy", "kitten", "狗", "猫", "寵物", "宠物")
_KIND_ASK_WORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("outdoor", ("风景", "風景", "风景照", "風景照", "landscape", "scenery",
                 "scenic", "outdoor view", "the view")),
    ("selfie", ("自拍", "selfie", "mirror pic", "mirror photo")),
    ("food", ("美食", "吃饭", "吃饭照", "food pic", "what you ate")),
    ("pet", ("宠物", "寵物", "小狗", "小猫", "my dog", "my cat")),
    ("indoor", ("室内", "室內", "indoor")),
)


def infer_scene_kind(parsed: Any, scene_class: str = "") -> str:
    """VLM 结论 + canonical 场景类 → kind；认不出 → other。纯函数。"""
    p = parsed if isinstance(parsed, dict) else {}
    objects = [str(o).lower() for o in (p.get("objects") or [])]
    blob = " ".join(objects) + " " + str(p.get("desc") or "").lower()
    if any(w in blob for w in _PET_OBJ):
        return "pet"
    cls = str(scene_class or p.get("scene") or "").strip().lower()
    if cls in _FOOD_CLASSES or any(w in blob for w in _FOOD_OBJ):
        return "food"
    indoor = str(p.get("indoor") or "").strip().lower()
    if cls in _OUTDOOR_CLASSES or indoor == "outdoor":
        return "outdoor"
    try:
        people = int(p.get("people_count") if p.get("people_count") is not None else -1)
    except (TypeError, ValueError):
        people = -1
    if "selfie" in blob or "mirror" in blob:
        return "selfie"
    if cls in _SELFIE_CLASSES and people >= 1:
        return "selfie"
    if indoor == "indoor" or cls in _INDOOR_CLASSES:
        return "indoor"
    if people >= 1:
        return "selfie"
    return "other"


def scene_kind_of(row: Optional[Dict[str, Any]]) -> str:
    """条目 kind：``tags kind:`` → auto_meta.scene_kind → 从场景类/识图结论推断。"""
    k = _tag_value(row, "kind:").lower()
    if k in SCENE_KINDS:
        return k
    am = (row or {}).get("auto_meta") if isinstance((row or {}).get("auto_meta"), dict) else {}
    ak = str((am or {}).get("scene_kind") or "").strip().lower()
    if ak in SCENE_KINDS:
        return ak
    return infer_scene_kind(am, row_scene_class(row))


def requested_scene_kind(text: Any) -> str:
    """客户点名的产品 kind；「风景」→ outdoor。认不出 → ""。"""
    t = normalize_text(text)
    if not t:
        return ""
    for kind, words in _KIND_ASK_WORDS:
        for w in words:
            if w in t:
                return kind
    return ""


def album_kind_counts(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """enabled 行按 kind 计数（A 段清单）。"""
    out = {k: 0 for k in SCENE_KINDS}
    for r in rows or []:
        if not r.get("enabled", True):
            continue
        k = scene_kind_of(r)
        if k in out:
            out[k] += 1
        else:
            out["other"] += 1
    return {k: n for k, n in out.items() if n}


def album_trigger_counts(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """三段计数：已确认 / AI 建议 / 无建议。"""
    confirmed = ai_suggest = none = 0
    for r in rows or []:
        trg = [str(x).strip() for x in (r.get("triggers") or []) if str(x or "").strip()]
        am = r.get("auto_meta") if isinstance(r.get("auto_meta"), dict) else {}
        sug = [str(x).strip() for x in ((am or {}).get("triggers_suggest") or [])
               if str(x or "").strip()]
        if trg:
            confirmed += 1
        elif sug:
            ai_suggest += 1
        else:
            none += 1
    return {"confirmed": confirmed, "ai_suggest": ai_suggest, "none": none}


def row_has_match_terms(row: Optional[Dict[str, Any]], *, suggest_ok: bool = True) -> bool:
    """正式触发词或（suggest_ok 时）建议词任一非空。"""
    r = row or {}
    if any(str(x or "").strip() for x in (r.get("triggers") or [])):
        return True
    if not suggest_ok:
        return False
    am = r.get("auto_meta") if isinstance(r.get("auto_meta"), dict) else {}
    return any(str(x or "").strip() for x in ((am or {}).get("triggers_suggest") or []))


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


# ── 配文时刻词守卫（2026-08-12 实锤）──────────────────────────────────────
# 凌晨 3 点发图，配文却是「下午的阳光刚好」——图可以是旧照（freshness=old 口径
# 允许），但**现在时态的时刻词**与发送时刻硬冲突＝当场穿帮。只认高置信显式
# 时刻词（早上/中午/下午/阳光正好…×深夜；凌晨/深夜/晚安…×白天），过去指涉
# （「上次下午拍的」）由词表天然放过大半，误杀代价只是回落中性配文，可接受。
_CAP_DAY_RE = re.compile(
    r"早上|早晨|清晨|上午|中午|下午|晌午|阳光|陽光|太阳|太陽|"
    r"\b(?:morning|noon|afternoon|sunshine|sunny)\b",
    re.IGNORECASE)
_CAP_NIGHT_RE = re.compile(
    r"凌晨|深夜|半夜|夜里|夜裡|晚安|夜景|月亮|月光|"
    r"\b(?:midnight|late\s+night|good\s*night|moonlight)\b",
    re.IGNORECASE)


def caption_tod_conflict(caption: Any, hour: Any) -> bool:
    """固定配文里的显式时刻词与当前小时是否硬冲突（纯函数）。

    与 ``tod_conflicts_with_hour`` 同分带：深夜 22-6 拦「白天词」配文、
    白天 6-17 拦「深夜词」配文、17-22 傍晚不判。无时刻词/判不了小时=不冲突。
    冲突时调用方应弃用该固定配文回落中性文案（图还发，只是话别说错）。
    """
    c = str(caption or "")
    if not c:
        return False
    try:
        h = int(hour)
    except (TypeError, ValueError):
        return False
    if h >= 22 or h < 6:
        return bool(_CAP_DAY_RE.search(c))
    if 6 <= h < 17:
        return bool(_CAP_NIGHT_RE.search(c))
    return False


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
    # 实施69 P3 实测补漏：「大腰子照片呢？」这类**光杆催讨**（名词+照片+呢，
    # 无看/发/拍动词）曾被 is_info_question 当成"问名词信息"→ 关键词池让路
    # → 刚入册的库存发不出（试触发端到端实测抓到）。「X照片呢」语义上就是
    # 在要图/催图，不是问「照片」是什么。
    r"(?:照片|相片|图|圖|视频|視頻|影片|自拍)\s*[呢咧勒]|"
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


# ── 邀约/否定守卫（2026-08-12 实锤）─────────────────────────────────────────
# 「出来喝咖啡」是**邀约**——客户约你出门，不是要看咖啡照片；触发词"咖啡"命中
# 关键词池 → 凌晨 3 点甩出一张白天咖啡馆照＝已读乱回+穿帮双杀（实录）。同理
# 「一起去看电影吧」「请你喝奶茶」都是社交动作，发图答非所问。否定句（「别再
# 发图了」）更是明确拒收。两类都让关键词池让路，交正常 LLM 答话（回应邀约本身）。
# 保守词表：邀约词必须是「共同行动」语义（出来/一起/约/请你/去不去…），单独
# 提到名词（「我在喝咖啡」）不拦——运营配触发词的本意就是这类分享场景。
# 显式求图动词优先放行：「出来玩之前先发张照片看看」仍是要图。
_INVITE_RE = re.compile(
    r"出[来來去]|一起|[约約](?:你|我|个|個|吗|嗎|不|[会會])?|"
    r"[请請]你|带[你我]|陪我|"
    r"去不去|来不来|來不來|要不要(?!看)|走不走|"
    r"咱们去|我们去|我們去|见面|見面|"
    r"\b(?:let'?s|join\s+me|come\s+(?:out|with|over)|wanna\s+go|"
    r"meet\s+(?:up|me))\b",
    re.IGNORECASE)
_MEDIA_DECLINE_RE = re.compile(
    r"[别別不][要再发發]?[发發传傳](?:图|圖|照|了)?|[别別]乱[发發]|"
    r"[发發]什么[发發]|够了|夠了|"
    r"\b(?:stop|don'?t)\s+send(?:ing)?\b",
    re.IGNORECASE)


def is_invite_or_decline(text: Any) -> bool:
    """客户消息是「邀约/拒收」语境 → True＝关键词池应让路（发图答非所问）。

    显式求图动词（``_MEDIA_REQ_RE``）优先——「出来玩前先发张自拍看看」仍要图；
    但拒收类（别发了）即使句里带"发"字也拦（词面重叠由否定表自身消化）。
    """
    t = normalize_text(text)
    if not t:
        return False
    if _MEDIA_DECLINE_RE.search(t):
        return True
    if _MEDIA_REQ_RE.search(t):
        return False
    return bool(_INVITE_RE.search(t))


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
    suggest_ok: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把候选行分成 (关键词命中, 建议词命中, 通用池)，同时过 enabled / 类型 / 关系闸门。

    Q-6 E：未确认建议词（``auto_meta.triggers_suggest``）低权重参与；``suggest_ok=False``
    （``album.autotag.apply=confirm``）时建议词不进匹配。
    """
    tn = normalize_text(text)
    mt_set = {str(m).strip().lower() for m in media_types} if media_types else None
    keyword: List[Dict[str, Any]] = []
    suggest: List[Dict[str, Any]] = []
    generic: List[Dict[str, Any]] = []
    for r in rows or []:
        if not r.get("enabled", True):
            continue
        if mt_set and str(r.get("media_type") or "").lower() not in mt_set:
            continue
        if bond_level is not None and int(r.get("min_bond_level") or 0) > int(bond_level):
            continue
        trg = [x for x in (r.get("triggers") or []) if str(x or "").strip()]
        am = r.get("auto_meta") if isinstance(r.get("auto_meta"), dict) else {}
        sug = [x for x in ((am or {}).get("triggers_suggest") or []) if str(x or "").strip()] if suggest_ok else []
        if trg and _triggers_hit(trg, tn):
            keyword.append(r)
        elif (not trg) and sug and _triggers_hit(sug, tn):
            rr = dict(r)
            rr["_match_weight"] = 0.6
            suggest.append(rr)
        elif not trg and not sug:
            generic.append(r)
        elif not trg and sug and not _triggers_hit(sug, tn):
            # 有建议词但本句未命中 → 不进通用池（有词就不是随机素材）
            pass
        elif trg and not _triggers_hit(trg, tn):
            pass
    return keyword, suggest, generic


def _weighted_choice(
    rows: Sequence[Dict[str, Any]], rng: Optional[random.Random] = None,
) -> Dict[str, Any]:
    r = rng or random
    weights = []
    for x in rows:
        base = max(1, int(x.get("weight") or 1))
        try:
            mw = float(x.get("_match_weight") or 1.0)
        except (TypeError, ValueError):
            mw = 1.0
        weights.append(max(0.1, base * mw))
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
    no_resend: bool = False,
    now_season: str = "",
    home_country: str = "",
    trace: Optional[Dict[str, Any]] = None,
    suggest_ok: bool = True,
    allow_random: bool = True,
    required_scene_kind: str = "",
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

    实施90 新增三闸（默认全关＝行为不变）：
    - ``no_resend``（resend_policy=strict）：层3「翻旧照」改为 **None**——同一
      会话绝不重发同图，素材耗尽交上游生成链出新图/诚实文字；
    - ``now_season``：人设当地当前季节——通用池剔除 ``season:`` 对立季条目
      （冬天绝不出盛夏海滩照；关键词池=运营显式意图，不受限）；
    - ``home_country``：人设所在国——通用池剔除 ``place:`` 异国条目（人在
      温哥华不轮播东京街景；要发旅行旧照请给它配触发词走关键词池）。
    - pHash 近重复族（条目带 ``phash`` 时自动生效）：排除面（软/硬）按指纹
      距离扩到「几乎同一张」的连拍/重编码——今天发 A、明天发 A 的近重复
      ＝复读实锤，一并挡。
    """
    def _tr(key: str, val: Any) -> None:
        if trace is not None:
            trace[key] = val

    def _tr_cut(gate: str, n: int) -> None:
        if trace is not None and n > 0:
            cut = trace.setdefault("cut", {})
            cut[gate] = int(cut.get(gate, 0)) + int(n)

    keyword, suggest, generic = _partition(
        rows, text, media_types=media_types, bond_level=bond_level,
        suggest_ok=bool(suggest_ok))
    # 信息性提问不劫持：疑问句且无求图动词 → 关键词池让路（见 is_info_question）。
    if keyword and is_info_question(text):
        keyword = []
    if suggest and is_info_question(text):
        suggest = []
    # 邀约/拒收不劫持：「出来喝咖啡」是约人不是要图（见 is_invite_or_decline）。
    if keyword and is_invite_or_decline(text):
        keyword = []
    if suggest and is_invite_or_decline(text):
        suggest = []
    kind = str(required_scene_kind or "").strip().lower()
    if kind in SCENE_KINDS:
        mt_set = {str(m).strip().lower() for m in media_types} if media_types else None
        keyword = [r for r in keyword if scene_kind_of(r) == kind]
        suggest = [r for r in suggest if scene_kind_of(r) == kind]
        generic = [r for r in generic if scene_kind_of(r) == kind]
        # 索「风景」：即使触发词字面没写「风景」，outdoor 存货仍可作候选（CK8HCT）。
        if not keyword and not suggest:
            kind_pool = []
            for r in rows or []:
                if not r.get("enabled", True):
                    continue
                if mt_set and str(r.get("media_type") or "").lower() not in mt_set:
                    continue
                if bond_level is not None and int(r.get("min_bond_level") or 0) > int(bond_level):
                    continue
                if scene_kind_of(r) == kind:
                    kind_pool.append(r)
            if kind_pool:
                suggest = kind_pool
    pool = keyword if keyword else (
        suggest if suggest else (
            generic if (generic_ok and allow_random) else []))
    from_generic = bool(not keyword and not suggest)
    _tr("pool", "keyword" if keyword else (
        "suggest" if suggest else (
            "generic" if (generic_ok and allow_random and generic) else "none")))
    _tr("start", len(pool))
    _tr("fallback", "random" if (from_generic and pool) else "none")
    if not pool:
        _tr("refused", "no_pool")
        return None
    # pHash 近重复族扩散（实施90）：排除过 A 就同时排除「几乎是 A」的连拍/
    # 重编码（软/硬两面同扩）；条目无指纹时按原 id 排除，零误伤软降级。
    _hard_seed = {str(x) for x in (hard_exclude_ids or ())}
    _soft_seed = {str(x) for x in (exclude_ids or ())}
    _n_hard0, _n_soft0 = len(_hard_seed), len(_soft_seed)
    if (_hard_seed or _soft_seed) and any(
            str((r or {}).get("phash") or "") for r in rows or []):
        try:
            from src.companion.image_phash import expand_ids_by_phash
            if _hard_seed:
                _hard_seed = expand_ids_by_phash(rows, _hard_seed)
            if _soft_seed:
                _soft_seed = expand_ids_by_phash(rows, _soft_seed)
        except Exception:
            pass
    _tr_cut("family",
            (len(_hard_seed) - _n_hard0) + (len(_soft_seed) - _n_soft0))
    # 重发冷却：硬排除，绝不逐层放宽（id 面 + 文件名面，两条链同一张图都算）。
    hx = _hard_seed
    if hx:
        _n0 = len(pool)
        pool = [r for r in pool if str(r.get("id")) not in hx]
        _tr_cut("cooldown", _n0 - len(pool))
        if not pool:
            _tr("refused", "cooldown")
            return None
    hxf = {_fold_file_key(x) for x in (hard_exclude_files or ())}
    hxf.discard("")
    if hxf:
        _n0 = len(pool)
        pool = [r for r in pool if row_file_key(r) not in hxf]
        _tr_cut("cooldown", _n0 - len(pool))
        if not pool:
            _tr("refused", "cooldown")
            return None
    # 场景硬要求（仅通用池）：点名要海边就只出海边，挑不到交回生成/文字。
    if required_scene_class and from_generic:
        _n0 = len(pool)
        pool = [r for r in pool if row_scene_class(r) == required_scene_class]
        _tr_cut("scene", _n0 - len(pool))
        if not pool:
            _tr("refused", "scene")
            return None
    # 季节/地点门（实施90，仅通用池——关键词池=运营显式意图不受限）：
    # 对立季（冬↔夏）与异国照片**硬剔除**，剔空 → None 交生成/诚实文字；
    # 无标注条目不受影响（标注只来自保守打标，宁可放过不误拦）。
    if from_generic and (now_season or home_country):
        try:
            from src.companion.media_taxonomy import (
                place_mismatch, row_place, row_season, season_tag_conflicts,
            )
            if now_season:
                _n0 = len(pool)
                pool = [r for r in pool
                        if not season_tag_conflicts(row_season(r), now_season)]
                _tr_cut("season", _n0 - len(pool))
                if not pool:
                    _tr("refused", "season")
                    return None
            if home_country:
                _n0 = len(pool)
                pool = [r for r in pool
                        if not place_mismatch(row_place(r), home_country)]
                _tr_cut("place", _n0 - len(pool))
                if not pool:
                    _tr("refused", "place")
                    return None
        except Exception:
            pass
    # 时段软过滤：白天照深夜不发（有标注才判；剔空放行——配文层兜底诚实）。
    if now_hour is not None:
        lit = [r for r in pool if not tod_conflicts_with_hour(row_tod(r), now_hour)]
        if lit:
            _tr_cut("tod", len(pool) - len(lit))
            pool = lit
        elif len(pool):
            _tr("tod_softened", True)
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
    ex_ids = _soft_seed
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
        # 层3：全发过 → strict 档（no_resend）**拒发**——同一会话绝不重发同图，
        # 上游回落生成链出新图/诚实文字；cooldown 档保旧行为「翻旧照」。
        if no_resend:
            _tr("refused", "strict_no_resend")
            return None
        return min(pool, key=lambda r: float(r.get("last_sent_at") or 0))
    return _weighted_choice(pool, rng)


def explain_match(
    rows: Sequence[Dict[str, Any]], text: str, *,
    generic_ok: bool = True,
    media_types: Optional[Sequence[str]] = None,
    bond_level: Optional[int] = None,
    now_hour: Optional[int] = None,
    now_season: str = "",
    home_country: str = "",
) -> Dict[str, Any]:
    """「试触发」用：返回这句话会命中哪个池 + 全部候选（不做加权随机，列全供预览）。

    实施90 全链 dry-run：传入 ``now_hour``/``now_season``/``home_country`` 时，
    每个候选带 ``blocked_by``（时段/季节/地点里会拦它的门；空=此刻可发）——
    运营自查从「猜为什么没发」变成「看哪道门拦的」。**会话级门**（重发冷却/
    防复读账本）与具体会话绑定，预览刻意不判（``conv_gates_included=False``）。
    季节/地点门与 select_media 同口径只约束通用池；关键词池仅时段参与提示。
    """
    keyword, suggest, generic = _partition(
        rows, text, media_types=media_types, bond_level=bond_level)
    info_q = bool(keyword or suggest) and is_info_question(text)
    if info_q:
        keyword = []
        suggest = []
    invite = bool(keyword or suggest) and is_invite_or_decline(text)
    if invite:
        keyword = []
        suggest = []
    if keyword:
        pool, cands = POOL_KEYWORD, keyword
    elif suggest:
        pool, cands = POOL_KEYWORD, suggest
    elif generic_ok and generic:
        pool, cands = POOL_GENERIC, generic
    else:
        pool, cands = POOL_NONE, []
    from_generic = pool == POOL_GENERIC

    def _blocked(c: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        try:
            if now_hour is not None and tod_conflicts_with_hour(
                    row_tod(c), now_hour):
                out.append("tod")
            if from_generic and (now_season or home_country):
                from src.companion.media_taxonomy import (
                    place_mismatch, row_place, row_season,
                    season_tag_conflicts,
                )
                if now_season and season_tag_conflicts(
                        row_season(c), now_season):
                    out.append("season")
                if home_country and place_mismatch(
                        row_place(c), home_country):
                    out.append("place")
        except Exception:
            return out
        return out

    return {
        "pool": pool,
        "info_question": info_q,
        "candidates": [
            {"id": c.get("id"), "media_type": c.get("media_type"),
             "url": c.get("url"), "thumb_url": c.get("thumb_url") or "",
             "caption": c.get("caption"),
             "triggers": c.get("triggers") or [], "weight": c.get("weight"),
             "tags": c.get("tags") or [], "hits": c.get("hits") or 0,
             "blocked_by": _blocked(c)}
            for c in cands
        ],
        "keyword_count": len(keyword),
        "generic_count": len(generic),
        "conv_gates_included": False,
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
    no_resend: bool = False,
    now_season: str = "",
    home_country: str = "",
    allow_random: bool = True,
    suggest_ok: bool = True,
    required_scene_kind: str = "",
    trace: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """store 便捷封装：取该人设 enabled 行 → select_media。store/persona 缺失 → None。

    ``conv_key`` 非空时启用防复读记忆：查该会话已发账本（id+系列）作排除面。
    ``resend_after_days``：账本时间衰减（默认 90 天后旧图重新可用；0=永久排除）。
    ``resend_cooldown_hours``（P0 一致性）：冷却窗内发过的条目硬排除（任何回落层
    都不重发）；``continuity_minutes``：最近一次发图在窗口内 → 优先同系列
    （同套衣服连拍），防「30 秒换一身衣服」穿帮。均需 ``conv_key``。
    ``no_resend``/``now_season``/``home_country``（实施90）：strict 绝不重发档 +
    季节/地点门，语义见 ``select_media``；调用方从 ``resolve_consistency_cfg``
    与 ``media_taxonomy.persona_geo_context`` 取值。
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
    _trace: Dict[str, Any] = trace if isinstance(trace, dict) else {}
    row = select_media(
        rows, text, generic_ok=generic_ok, media_types=media_types,
        avoid_id=avoid_id, bond_level=bond_level, rng=rng,
        exclude_ids=ex_ids, exclude_series=ex_series,
        now_hour=now_hour, required_scene_class=required_scene_class,
        hard_exclude_ids=hard_ids, hard_exclude_files=hard_files,
        prefer_series=prefer_series, no_resend=bool(no_resend),
        now_season=str(now_season or ""),
        home_country=str(home_country or ""), trace=_trace,
        suggest_ok=bool(suggest_ok), allow_random=bool(allow_random),
        required_scene_kind=str(required_scene_kind or ""),
    )
    # 拦截观测（实施90）：A/B 两条链都从这里过——「为什么没发」进程计数
    try:
        from src.companion.album_gate_stats import record as _gate_record
        _gate_record(_trace, row is not None)
    except Exception:
        pass
    # 语义召回·影子（实施90，默认关）：泛化要图却 miss（触发词字面没接住）→
    # 后台线程记「如果按语义召回会中哪张」；配置读取/嵌入全在线程里，热路零成本。
    if row is None and generic_ok and str(_trace.get("refused") or "") == "no_pool":
        try:
            from src.companion.album_semantic_recall import maybe_shadow
            maybe_shadow(store, persona_id, text)
        except Exception:
            pass
    return row


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
    "POOL_KEYWORD", "POOL_GENERIC", "POOL_NONE", "SCENE_CLASSES", "SCENE_KINDS",
    "normalize_text", "select_media", "explain_match", "pick_media", "caption_for",
    "scene_class_of", "tod_conflicts_with_hour", "row_tod", "row_scene_class",
    "row_file_key", "series_of", "is_info_question", "is_invite_or_decline",
    "caption_tod_conflict",
    "infer_scene_kind", "scene_kind_of", "requested_scene_kind",
    "album_kind_counts", "album_trigger_counts", "row_has_match_terms",
]
