"""今日话题包：每天给 AI 一小撮「今天真实的新鲜事」（RSS → 缓存 → prompt 素材）。

设计目标：冷场/被问起「最近有什么新鲜事」时，AI 能自然聊起当天真实发生的事，
杜绝「天天只有抹茶店」。stdlib-only（urllib + xml.etree），零第三方依赖。

缓存文件 ``config/daily_topics_cache.json`` 是 **CWD 相对路径**（本仓 C 类惯例，
与 ``config/album_restock_plan.json`` 同口径）：生产进程 CWD = 实例数据根，
缓存恰好落实例数据区；测试一律经 ``cache_path`` 参数重定向 tmp_path，
绝不写仓库 ``config/``。

默认 feeds（2026-08-02 本机 curl 实测）：
  - Google News 中文（200，RSS 有效）
  - BBC 中文（301 → trad/rss.xml，urllib 自动跟随重定向，RSS 有效）
  - 知乎 ``https://www.zhihu.com/rss`` 实测 200 但空 body，**不可用未选**。
运营可经 ``companion.daily_topics.feeds`` 覆写。

2026-08-04 群发事故沉淀（198 打包机实锤：「伊朗阿曼接近达成霍尔木兹海峡
航行协议」被 news_share 在 73 分钟内发给 9 个客户，含 BotFather）：
  - ``pick_topics_for(variety_key=)``——轮换盐加联系人键，不同客户分散到
    整个话题池（旧键只含 日期#标题 ⇒ 全员同题）；
  - ``smalltalk_topic``——主动开场只认「轻话题」允许清单（体育/文娱/生活
    方式…），军政财法灾等识别不出类别的一律排除（屏蔽词表在真实数据上
    双向漏：霍尔木兹/海军扫雷/被开除党籍全都穿过了 blocklist）；
  - ``build_topics_hint`` 与 news_share directive 加反编造钉子（素材只有
    一句标题，细节不知道、别编、被追问就承认没细看）。

配置 ``companion.daily_topics``（默认 ``enabled: false``，本仓新子系统惯例）。
接线：``skill_manager._inject_reply_freshness`` → ``_daily_topics_hint`` →
``ai_client._build_context_prompt`` 消费。
"""
from __future__ import annotations

import difflib
import html as _html
import json
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import closing
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zlib import crc32

DEFAULT_CACHE_PATH = "config/daily_topics_cache.json"

# 实测可达的默认源（见模块 docstring；全不可达时运营需自配 feeds）
DEFAULT_FEEDS = (
    "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
)

# 闲聊场景避重就轻的保守默认屏蔽词（灾难/暴力/战争/政治人物——宁可漏进不错进）。
# 中文词按包含匹配；纯拉丁词按词边界匹配（防 dead→deadline 误伤）。
DEFAULT_BLOCKLIST = (
    "战争", "开战", "停火", "武装", "军演", "导弹", "轰炸", "核武", "空袭",
    "袭击", "恐袭", "恐怖", "阅兵", "战机", "军队", "交火", "叛乱", "政变",
    "地震", "海啸", "洪灾", "泥石流", "山火", "空难", "坠机", "沉船", "矿难",
    "爆炸", "火灾", "灾区", "遇难", "遇害", "凶杀", "谋杀", "杀人", "命案",
    "枪击", "抢劫", "绑架", "强奸", "自杀", "身亡", "丧生", "死亡", "死者",
    "尸体", "坠亡", "溺亡", "车祸", "疫情", "病毒", "确诊", "瘟疫",
    "示威", "抗议", "骚乱", "暴乱", "制裁", "间谍",
    "特朗普", "拜登", "普京", "泽连斯基", "金正恩", "马克龙", "内塔尼亚胡",
    "哈梅内伊", "马科斯", "习近平", "李强", "国防部", "外交部", "白宫",
    "克里姆林宫", "五角大楼",
    "war", "missile", "bomb", "bombing", "earthquake", "crash", "killed",
    "murder", "shooting", "dead", "death", "terror", "explosion", "suicide",
    "trump", "biden", "putin", "zelensky",
)

_TRIGGER_RE = re.compile(
    r"(无聊|聊点|新鲜事|新闻|最近有什么|有什么好玩|干嘛呢|在干嘛|忙什么"
    r"|bored|what'?s\s*up|anything\s+new)",
    re.IGNORECASE,
)

# 直球新闻问句（2026-08-12 实锤：「有什么新闻吗」被 AI 答「没什么特别的新闻」
# ——素材明明在缓存里，弱指令「仅当对方问起时提其中一件」被 LLM 当可选项忽略）。
# 命中 → build_topics_hint 走 direct_ask 强指令变体。比 _TRIGGER_RE 窄：只认
# 明确在「要新闻/新鲜事/大事」的句子，泛闲聊（在干嘛/无聊）仍走弱指令。
_NEWS_ASK_RE = re.compile(
    r"新闻|新鮮事|新鲜事|大事|时事|時事|热搜|熱搜|头条|頭條"
    r"|发生(?:了)?(?:什么|啥)|世界上.{0,6}(?:什么|啥)事"
    r"|\b(?:news|headlines?|what(?:'s| is) happening)\b",
    re.IGNORECASE,
)


def is_news_question(text: Any) -> bool:
    """本条入站是否在**直球问新闻**（要素材且期待正面作答）。纯函数。"""
    return bool(_NEWS_ASK_RE.search(str(text or "")))

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_LATIN_WORD_RE = re.compile(r"^[a-z0-9 '\-]+$")

# 轻话题允许清单（主动开场 allowlist）：体育/文娱/生活方式/文化科普/萌宠/
# 乡土温情/天气奇观。与 DEFAULT_BLOCKLIST 的「屏蔽」哲学相反——主动寒暄
# 拿错话题的代价（2026-08-04 实锤：霍尔木兹海峡地缘新闻 73 分钟内群发 9 个
# 客户）远高于漏掉一条好话题（回落天气/问候），故识别不出类别的一律不合格。
# ⚠ 选词避开多义冲突：「跳水」（体育∩股市暴跌）、「卫星」（科普∩军用）、
# 「火箭」（航天∩武器）、「手机」（数码∩媒体品牌「手机新浪网」——Google
# News 聚合摘要会拼进媒体名，实测让休达边境新闻误判为轻话题）不进表。
SMALLTALK_TOPIC_WORDS = (
    # 体育赛事
    "体育", "赛事", "比赛", "夺冠", "冠军", "联赛", "球迷", "足球", "篮球",
    "网球", "乒乓", "羽毛球", "排球", "高尔夫", "马拉松", "夜跑", "慢跑",
    "健身", "瑜伽", "滑雪", "冲浪", "骑行", "登山", "徒步", "露营",
    "奥运", "亚运", "世界杯", "锦标赛", "公开赛", "电竞", "晋级",
    "决赛", "半决赛", "夺金", "游泳", "田径", "体操",
    # 文娱
    "电影", "影片", "票房", "影展", "剧集", "电视剧", "短剧", "综艺",
    "演唱会", "音乐节", "音乐会", "新歌", "专辑", "歌手", "乐队",
    "动漫", "动画", "游戏", "手游", "玩家", "演出", "话剧", "舞台剧",
    "舞蹈", "街舞", "霹雳舞", "纪录片",
    # 2026-08-16 四源试点召回补词：垂直娱乐源大量条目此前判不出类别
    # （《藏锋》主演阵容/《披荆斩棘》开播 等实测 no-hit → 主动开场白白饿死）。
    # 全部选无歧义强娱乐词，军政财新闻不会出现。
    "上映", "首映", "热映", "定档", "开播", "主演", "演员", "新剧", "巡演",
    # 生活方式
    "美食", "餐厅", "小吃", "咖啡", "奶茶", "甜品", "烘焙", "夜市",
    "集市", "市集", "旅游", "旅行", "游客", "景区", "打卡", "民宿",
    "赏花", "樱花", "灯会", "庙会", "花展", "书展", "假期", "黄金周",
    # 文化科普 / 萌宠 / 科技生活
    "展览", "博物馆", "美术馆", "文创", "考古", "天文", "流星雨",
    "日食", "月食", "超级月亮", "航天",
    "动物", "熊猫", "萌宠", "动物园", "水族馆", "宠物",
    "新机", "数码", "机器人", "人工智能", "短视频",
    # 乡土温情 / 公益
    "乡村", "支教", "公益", "志愿", "反哺",
    # 天气奇观（轻）
    "初雪", "极光", "彩虹",
    # 拉丁词（词边界匹配）
    "coffee", "movie", "film", "music", "concert", "festival", "food",
    "travel", "sports", "match", "tournament", "anime", "pet",
    "nba", "wta", "atp", "esports",
)

# ── 实施84 P2（2026-08-29 老板拍板的内容策略）─────────────────────────────────
# ① 不做球队赛事类分享——词表只含「赛事/球队」语义（比赛、联赛、世界杯…），
#   刻意不含健身/瑜伽/骑行/露营等生活方式词（那些仍是正常闲聊素材）；
# ② 社会/娱乐新闻为主——prefer_kinds 参与选题排序（entertainment/news 靠前）；
# ③ 新闻以用户所在国家为准——cfg_for_user_region（见下）。
SPORTS_EVENT_WORDS = (
    "体育", "赛事", "比赛", "夺冠", "冠军", "联赛", "球迷", "球队", "足球",
    "篮球", "网球", "乒乓", "羽毛球", "排球", "高尔夫", "马拉松",
    "奥运", "亚运", "世界杯", "锦标赛", "公开赛", "电竞", "晋级",
    "决赛", "半决赛", "夺金", "游泳", "田径", "体操", "球员", "球赛",
    "nba", "wta", "atp", "esports", "sports", "match", "tournament",
)


def is_sports_event_topic(topic: Any) -> bool:
    """该条目是否「球队赛事类」（kind=sports 的垂直源条目，或标题/摘要命中
    赛事词）。纯函数，供主动分享与素材注入按内容策略剔除。"""
    if not isinstance(topic, dict):
        return False
    if str(topic.get("kind") or "").strip().lower() == "sports":
        return True
    blob = (str(topic.get("title") or "")
            + " " + str(topic.get("summary") or "")).lower()
    return _match_any(blob, SPORTS_EVENT_WORDS)


def _kind_rank(topic: Dict[str, Any], prefer_kinds) -> int:
    """类别偏好序：prefer_kinds 里的类别按位次靠前，未列出的排最后。"""
    kind = str(topic.get("kind") or "news").strip().lower()
    try:
        return list(prefer_kinds or ()).index(kind)
    except ValueError:
        return len(prefer_kinds or ())


# 重话题否决表（veto，优先级高于轻词命中）：军政地缘/官员任免/宏观财经/
# 灾难事故/边境移民。存在理由＝Google News 聚合摘要会把媒体品牌名（如
# 「手机新浪网」）拼进 summary，轻词表会被这类噪声误点亮（2026-08-04
# 休达边境新闻实测）；否决方向的误判只是少发一条素材（安全侧），故可
# 放心扫 标题+摘要。与 DEFAULT_BLOCKLIST 互补：那份在**抓取清洗**时挡，
# 这份在**主动开场消费**时挡（旧缓存/自配 feeds 也被兜住，纵深防御）。
HEAVY_VETO_WORDS = (
    "军事", "美军", "海军", "空军", "停战", "战前", "海峡",
    "边境", "边界", "管控", "移民", "难民", "危机",
    "党籍", "纪委", "双开", "免职", "辞去", "落马", "被查",
    "涉嫌", "判刑", "庭审", "起诉", "逮捕",
    "美联储", "央行", "汇率", "加息", "降息", "关税", "国债",
    "期货", "涨停", "跌停", "午盘", "收盘",
    "灾害", "崩塌", "火山", "喷发", "疏散", "出入境",
    # 2026-08-16 四源试点首刷实测补词：IPO/停复牌类硬财经与党建栏目稿穿过了
    # 上面的财经词族（词表原本只盯行情/宏观）。陪伴闲聊不聊这两类；只收
    # 中文锚词——ETF/IPO 这类拉丁词在中文紧邻语境下词边界匹配不到（\b 在
    # 汉字与字母间不成立），同一条目由「停牌/科创板」等中文词兜住。
    "科创板", "新股", "停牌", "复牌", "上交所", "深交所",
    "治党", "党建", "廉政", "巡视", "铸魂",
)

_MAX_FEED_BYTES = 2 * 1024 * 1024
_RETRY_MIN_SEC = 600.0  # 抓取失败后的最小重试间隔（防每条消息都触发重抓）

# 刷新并发防重（按缓存路径分桶；daemon 线程 + in_flight 旗标）
_REFRESH_LOCK = threading.Lock()
_REFRESH_STATE: Dict[str, Dict[str, Any]] = {}

# last_offer 进程内账本（bounded，防会话数无限增长）
_OFFER_LOCK = threading.Lock()
_OFFER_TS: Dict[str, float] = {}
_OFFER_CAP = 400


# ── 抓取与清洗（纯函数 + 可注入 urlopen）───────────────────────────────────────
def _default_urlopen(url: str, timeout: float):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (chatx-daily-topics)"})
    return urllib.request.urlopen(req, timeout=timeout)  # nosec: 运营配置的 RSS 源


def _strip_html(text: str) -> str:
    """去 HTML 标签 + 实体（Google News 的 description 是转义过的 HTML）。"""
    s = _html.unescape(str(text or ""))
    s = _TAG_RE.sub(" ", s)
    s = _html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def _parse_ts(raw: str) -> float:
    """RFC822（RSS pubDate）/ ISO8601（Atom published/updated）→ epoch；失败 0。"""
    s = str(raw or "").strip()
    if not s:
        return 0.0
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return float(dt.timestamp())
    except Exception:
        pass
    try:
        from datetime import datetime
        return float(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0.0


def _child_text(el, names) -> str:
    """按 localname（大小写不敏感、剥 namespace）取第一个匹配子元素文本。"""
    for child in el:
        tag = str(child.tag).rsplit("}", 1)[-1].lower()
        if tag in names:
            txt = (child.text or "").strip()
            if txt:
                return txt
            # Atom <link href="..."/> 形态
            href = str(child.get("href") or "").strip()
            if href:
                return href
    return ""


def fetch_feed(
    url: str,
    *,
    timeout: float = 8,
    urlopen_fn: Optional[Callable[..., Any]] = None,
) -> List[Dict[str, Any]]:
    """抓取并解析一个 RSS/Atom 源 → ``[{title, summary, link, published_ts}]``。

    ``urlopen_fn(url, timeout)`` 可注入（测试用 fake，返回 file-like）。
    任何异常（网络/解析/编码）返回 ``[]``，绝不抛。
    """
    try:
        opener = urlopen_fn or _default_urlopen
        with closing(opener(url, timeout)) as resp:
            data = resp.read(_MAX_FEED_BYTES)
        root = ET.fromstring(data)
        items: List[Dict[str, Any]] = []
        for el in root.iter():
            tag = str(el.tag).rsplit("}", 1)[-1].lower()
            if tag not in ("item", "entry"):
                continue
            title = _child_text(el, ("title",))
            if not title:
                continue
            items.append({
                "title": title,
                "summary": _child_text(el, ("description", "summary", "content")),
                "link": _child_text(el, ("link",)),
                "published_ts": _parse_ts(
                    _child_text(el, ("pubdate", "published", "updated", "date"))),
            })
        return items
    except Exception:
        return []


def _norm_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", str(title or "").lower())


def _word_hit(low: str, word: str) -> bool:
    """单词匹配：中文按包含、纯拉丁词按词边界（防 dead→deadline 误伤）。"""
    w = str(word or "").strip().lower()
    if not w:
        return False
    if _LATIN_WORD_RE.match(w):
        return re.search(r"\b" + re.escape(w) + r"\b", low) is not None
    return w in low


def _match_any(low: str, words) -> bool:
    return any(_word_hit(low, w) for w in (words or ()))


def _blocked(text: str, blocklist) -> bool:
    return _match_any(str(text or "").lower(), blocklist or ())


# ── 话题类别（kind）：按源 URL 推断，供分类分发/观测（2026-08-16 P2）─────────
# 设计：kind 长在**源**上而不是逐条分类——垂直源（Google News 主题 feed 等）
# 天然定类，零 LLM 零词表歧义；综合源一律 "news"。URL 子串识别 + 配置覆写
# ``companion.daily_topics.feed_kinds: {url子串: kind}``（覆写优先）。
# 刻意不改 feeds 配置形态（保持字符串列表）：dict 形态会让「新配置+旧代码」
# 的中间态把 URL 变成垃圾串（str(dict)），字符串列表对新旧代码都成立。
_URL_KIND_PATTERNS = (
    ("topic/entertainment", "entertainment"),
    ("topic/sports", "sports"),
    ("topic/technology", "tech"),
    ("topic/science", "science"),
    ("topic/health", "health"),
    ("topic/business", "business"),
)


def feed_kind(url: str, kind_map: Optional[Dict[str, str]] = None) -> str:
    """源 URL → 话题类别；覆写 map 子串命中优先，识别不出返回 "news"。纯函数。"""
    low = str(url or "").lower()
    if isinstance(kind_map, dict):
        for sub, kind in kind_map.items():
            s = str(sub or "").strip().lower()
            k = str(kind or "").strip().lower()
            if s and k and s in low:
                return k
    for sub, kind in _URL_KIND_PATTERNS:
        if sub in low:
            return kind
    return "news"


def smalltalk_verdict(topic: Dict[str, Any]) -> Dict[str, Any]:
    """轻话题判定的可解释版本：``{ok, veto_hits, light_hits}``。

    ``smalltalk_topic`` 的单一事实源；周审 CLI（scripts/topic_gate_report）
    靠 hits 字段回答「这条为什么被拦/放行」——词表校准的读数入口。
    """
    if not isinstance(topic, dict):
        return {"ok": False, "veto_hits": [], "light_hits": []}
    blob = (str(topic.get("title") or "")
            + " " + str(topic.get("summary") or "")).lower()
    veto = [w for w in HEAVY_VETO_WORDS if _word_hit(blob, w)]
    veto += [w for w in DEFAULT_BLOCKLIST
             if w not in veto and _word_hit(blob, w)]
    light = [w for w in SMALLTALK_TOPIC_WORDS if _word_hit(blob, w)]
    return {
        "ok": not veto and bool(light),
        "veto_hits": veto,
        "light_hits": light,
    }


def smalltalk_topic(topic: Dict[str, Any]) -> bool:
    """轻话题判定：重词否决（含 DEFAULT_BLOCKLIST 纵深）→ 轻词命中才合格。

    供**主动开场**消费（news_share）：军政财法灾类硬新闻默认不合格——
    识别不出类别的一律排除，宁可回落天气/问候也不拿地缘政治当寒暄。
    否决先于允许：聚合摘要里的媒体品牌噪声（「手机新浪网」）可能误点亮
    轻词，但同一条摘要里的重词会先把它否决掉。
    反应式素材注入刻意**不**走本判定（对方主动问「最近有什么新闻」时
    聊大新闻是自然的，且反应式一次给 3 条由 LLM 择用，风险面不同）。
    """
    return bool(smalltalk_verdict(topic)["ok"])


def sanitize_topics(
    items: List[Dict[str, Any]],
    *,
    blocklist=None,
    max_age_hours: float = 48,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """清洗条目：去 HTML → 屏蔽词剔除 → 标题相似度去重 → 时间新到旧。

    ``blocklist=None`` 用内置保守默认表；传入即为完整表（调用方自己决定
    是否含默认表——``parse_topics_cfg`` 已输出「默认+追加」的合并表）。
    """
    now_v = float(now if now is not None else time.time())
    block = DEFAULT_BLOCKLIST if blocklist is None else blocklist
    kept: List[Dict[str, Any]] = []
    kept_norms: List[str] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = _strip_html(it.get("title") or "")
        if not title:
            continue
        summary = _strip_html(it.get("summary") or "")[:400]
        ts = float(it.get("published_ts") or 0.0)
        if ts > 0 and max_age_hours > 0 and (now_v - ts) > max_age_hours * 3600:
            continue
        if _blocked(title + " " + summary, block):
            continue
        norm = _norm_title(title)
        dup = False
        for existing in kept_norms:
            if norm == existing or difflib.SequenceMatcher(
                    None, norm, existing).ratio() >= 0.6:
                dup = True
                break
        if dup:
            continue
        kept_norms.append(norm)
        kept.append({
            "title": title,
            "summary": summary,
            "link": str(it.get("link") or ""),
            "published_ts": ts,
            # kind 透传（旧缓存/未标注条目回落 "news"，向后兼容）
            "kind": str(it.get("kind") or "news"),
        })
    kept.sort(key=lambda d: -float(d.get("published_ts") or 0.0))
    return kept


# ── 缓存（CWD 相对 config/，C 类落点；测试经 cache_path 注入 tmp）──────────────
def read_topics_cache(cache_path: Optional[str] = None) -> Dict[str, Any]:
    """读缓存文件；任何异常返回 ``{}``。"""
    try:
        p = Path(str(cache_path or "") or DEFAULT_CACHE_PATH)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cache(cache_path: str, data: Dict[str, Any]) -> None:
    p = Path(str(cache_path or "") or DEFAULT_CACHE_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")


def refresh_now(
    cfg: Dict[str, Any],
    *,
    now: Optional[float] = None,
    urlopen_fn: Optional[Callable[..., Any]] = None,
    cache_path: Optional[str] = None,
) -> bool:
    """同步抓取全部 feeds → sanitize → 写缓存。全败（0 条）保留旧缓存返回 False。"""
    path = str(cache_path or cfg.get("cache_path") or DEFAULT_CACHE_PATH)
    feeds = [str(u) for u in (cfg.get("feeds") or ()) if str(u or "").strip()]
    if not feeds:
        return False
    items: List[Dict[str, Any]] = []
    for url in feeds:
        kind = feed_kind(url, cfg.get("feed_kinds"))
        for it in fetch_feed(url, urlopen_fn=urlopen_fn):
            it["kind"] = kind
            items.append(it)
    topics = sanitize_topics(
        items, blocklist=cfg.get("blocklist"), now=now,
    )[: int(cfg.get("max_items") or 8)]
    if not topics:
        return False  # 抓取全败/全被剔除 → 保留旧缓存
    try:
        _write_cache(path, {
            "fetched_ts": float(now if now is not None else time.time()),
            "topics": topics,
        })
        return True
    except Exception:
        return False


def refresh_if_stale(
    cfg: Dict[str, Any],
    *,
    now: Optional[float] = None,
    urlopen_fn: Optional[Callable[..., Any]] = None,
    cache_path: Optional[str] = None,
    background: bool = True,
) -> bool:
    """缓存过期则起 daemon 线程刷新（绝不阻塞调用方）；新鲜/在刷中直接返回。

    返回「本次是否发起了刷新」。``background=False`` 供测试同步执行。
    模块级 in_flight 旗标（按缓存路径分桶）防并发重复刷；抓取失败后
    ``_RETRY_MIN_SEC`` 内不重试（防每条消息都触发重抓）。
    """
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return False
    path = str(cache_path or cfg.get("cache_path") or DEFAULT_CACHE_PATH)
    feeds = [str(u) for u in (cfg.get("feeds") or ()) if str(u or "").strip()]
    if not feeds:
        return False
    now_v = float(now if now is not None else time.time())
    refresh_hours = float(cfg.get("refresh_hours") or 6)
    cache = read_topics_cache(path)
    fetched_ts = float(cache.get("fetched_ts") or 0.0)
    if fetched_ts > 0 and (now_v - fetched_ts) < refresh_hours * 3600:
        return False
    with _REFRESH_LOCK:
        state = _REFRESH_STATE.setdefault(
            path, {"in_flight": False, "last_attempt": 0.0})
        if state["in_flight"]:
            return False
        if (now_v - float(state["last_attempt"])) < _RETRY_MIN_SEC:
            return False
        state["in_flight"] = True
        state["last_attempt"] = now_v

    def _worker() -> None:
        try:
            refresh_now(
                cfg, now=now, urlopen_fn=urlopen_fn, cache_path=path)
        except Exception:
            pass
        finally:
            with _REFRESH_LOCK:
                state["in_flight"] = False

    if background:
        threading.Thread(
            target=_worker, name="daily-topics-refresh", daemon=True).start()
    else:
        _worker()
    return True


# ── 选题 / 触发 / 渲染 ────────────────────────────────────────────────────────
def pick_topics_for(
    persona_tastes: Optional[List[str]],
    k: int = 3,
    *,
    now: Optional[float] = None,
    cache: Optional[Dict[str, Any]] = None,
    cache_path: Optional[str] = None,
    variety_key: str = "",
    secondary_tastes: Optional[List[str]] = None,
    exclude_sports: bool = False,
    prefer_kinds: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """从缓存挑 ≤k 条话题：兴趣词命中的优先，其余按 crc32 当日确定性轮换
    补足（今天恒定、明天自动换）。返回 ``[{title, summary}]``。

    ``variety_key``（如联系人键）参与轮换盐：同一联系人当日恒定、不同
    联系人分散到整个话题池——修 2026-08-04 实锤「同一条霍尔木兹新闻
    73 分钟内群发 9 个客户」（旧轮换键只含日期+标题，全员同题）。
    省略时保持旧键格式（日期#标题），既有调用方零行为变化。

    实施84 P2 增量（默认值＝旧行为，既有调用方零变化）：
    - ``secondary_tastes``：次级兴趣词——主词（如**用户**聊天里的兴趣）命中的
      排最前，次词（如人设口味）命中的其次，其余轮换补足；
    - ``exclude_sports``：剔除球队赛事类条目（老板拍板「不做赛事分享」）；
    - ``prefer_kinds``：类别偏好序（如 ["entertainment","news"]＝社会/娱乐
      为主），同优先层内先按类别再按轮换盐排。
    """
    data = cache if isinstance(cache, dict) else read_topics_cache(cache_path)
    topics = [t for t in (data.get("topics") or []) if isinstance(t, dict)]
    if exclude_sports and topics:
        topics = [t for t in topics if not is_sports_event_topic(t)]
    if not topics:
        return []
    now_v = float(now if now is not None else time.time())
    day = time.strftime("%Y-%m-%d", time.localtime(now_v))
    vk = str(variety_key or "").strip()
    tastes = [str(w or "").strip().lower() for w in (persona_tastes or [])]
    tastes = [w for w in tastes if w]
    tastes2 = [str(w or "").strip().lower() for w in (secondary_tastes or [])]
    tastes2 = [w for w in tastes2 if w]

    def _matches(t: Dict[str, Any], words) -> bool:
        blob = (str(t.get("title") or "") + " " + str(t.get("summary") or "")).lower()
        return any(w in blob for w in words)

    def _rot(t: Dict[str, Any]) -> int:
        title = t.get("title") or ""
        key = f"{day}#{vk}#{title}" if vk else f"{day}#{title}"
        return crc32(key.encode("utf-8", "ignore"))

    def _order_key(t: Dict[str, Any]):
        if prefer_kinds:
            return (_kind_rank(t, prefer_kinds), _rot(t))
        return _rot(t)

    matched = [t for t in topics if _matches(t, tastes)] if tastes else []
    matched_ids = {id(t) for t in matched}
    matched2 = ([t for t in topics
                 if id(t) not in matched_ids and _matches(t, tastes2)]
                if tastes2 else [])
    matched2_ids = {id(t) for t in matched2}
    rest = [t for t in topics
            if id(t) not in matched_ids and id(t) not in matched2_ids]
    if len(matched) > 1 and (vk or prefer_kinds):
        # 同人设多条兴趣命中时也按联系人分散（否则该人设全部联系人仍同题）
        matched.sort(key=_order_key)
    if len(matched2) > 1:
        matched2.sort(key=_order_key)
    rest.sort(key=_order_key)
    keep = max(1, int(k))
    picked = (matched + matched2 + rest)[:keep]
    return [
        {
            "title": str(t.get("title") or "")[:120],
            "summary": str(t.get("summary") or "")[:80],
            "kind": str(t.get("kind") or "news"),
        }
        for t in picked
    ]


def should_offer_topics(
    inbound_text: str,
    last_offer_ts: float,
    *,
    now: Optional[float] = None,
    cooldown_hours: float = 4,
    offer_percent: float = 15,
) -> bool:
    """是否该在本轮附上话题素材。

    入站命中触发词（无聊/新鲜事/在干嘛/bored/what's up…）→ True（无视冷却）；
    否则冷却期外按 crc32(日期+小时+入站文本) % 100 < ``offer_percent``
    概率性 True（确定性可测：同一小时同文本恒定）。``offer_percent`` 缺省 15
    ＝旧行为；实施55「新闻为主」的部署经配置调高（zhiliao overlay 35）。
    """
    text = str(inbound_text or "")
    if _TRIGGER_RE.search(text):
        return True
    now_v = float(now if now is not None else time.time())
    try:
        if float(last_offer_ts or 0.0) > 0 and (
                now_v - float(last_offer_ts)) < float(cooldown_hours) * 3600:
            return False
    except (TypeError, ValueError):
        pass
    try:
        pct = max(0.0, min(100.0, float(offer_percent)))
    except (TypeError, ValueError):
        pct = 15.0
    lt = time.localtime(now_v)
    key = f"{time.strftime('%Y-%m-%d', lt)}#{lt.tm_hour}#{text.strip()}"
    return crc32(key.encode("utf-8", "ignore")) % 100 < pct


def build_topics_hint(
    topics: List[Dict[str, Any]], lang: str = "zh", *,
    direct_ask: bool = False,
) -> str:
    """把选出的话题渲染成注入 prompt 的「今日新鲜事·可选素材」块；空列表返回 ""。

    ``direct_ask=True``（对方直球问新闻，见 ``is_news_question``）→ 强指令变体：
    「必须挑一两件正面回答，禁答『没什么新闻』」。弱指令实测会被 LLM 当可选项
    忽略（2026-08-12：素材在场仍答「没什么特别的新闻」）。
    """
    rows = [t for t in (topics or []) if isinstance(t, dict) and t.get("title")]
    if not rows:
        return ""
    zh = not str(lang or "zh").lower().startswith("en")
    parts: List[str] = []
    for i, t in enumerate(rows, 1):
        title = str(t.get("title") or "").strip()
        summary = str(t.get("summary") or "").strip()[:80]
        seg = f"{i}) {title}"
        if summary and _norm_title(summary) != _norm_title(title):
            seg += f"——{summary}" if zh else f" — {summary}"
        parts.append(seg)
    if zh:
        if direct_ask:
            return (
                "【今日新鲜事·对方正在问你】对方这条消息就是在问最近的新闻/"
                "新鲜事，你今天刷到过这些：" + "；".join(parts)
                + "。从中挑一两件用你自己的口吻自然说出来正面回答——**不要**说"
                "「没什么新闻」「没关注」这类敷衍话（你手上明明有素材）。"
                "别像播报新闻，别罗列全部。素材只有一句话标题，标题之外的"
                "细节你并不知道——不要编造具体数字、进展或结果；被追问细节"
                "就坦然说只是刷到标题没细看。"
            )
        return (
            "【今日新鲜事·可选素材】今天可以自然聊起的事：" + "；".join(parts)
            + "。仅当话题冷下来或对方问起时提其中一件，用你自己的口吻和人设"
            "视角说，别像播报新闻，别一次提多件，别硬转话题。"
            "素材只有这些一句话标题，标题之外的细节你并不知道——不要编造"
            "具体数字、进展或结果；被追问细节就坦然说只是刷到标题没细看。"
        )
    if direct_ask:
        return (
            "[Today's fresh topics — they are ASKING you] The user's message "
            "is directly asking about recent news. You did see these today: "
            + "; ".join(parts)
            + ". Pick one or two and answer them naturally in your own voice "
            "— do NOT say 'nothing special' or 'haven't been following' (you "
            "clearly have material). Don't sound like a news anchor, don't "
            "list everything. You only know these one-line headlines — never "
            "invent extra details, numbers or outcomes; if pressed for "
            "specifics, admit you only skimmed the headline."
        )
    return (
        "[Today's fresh topics — optional material] Things you could naturally "
        "bring up today: " + "; ".join(parts)
        + ". Only mention ONE of them, and only when the conversation cools "
        "down or you're asked; say it in your own voice from your persona's "
        "perspective — don't sound like a news anchor, don't list several at "
        "once, don't force a topic change. You only know these one-line "
        "headlines — never invent extra details, numbers or outcomes; if "
        "pressed for specifics, admit you only skimmed the headline."
    )


def build_no_topics_hint(lang: str = "zh") -> str:
    """直球问新闻但缓存无货时的诚实兜底提示（防 LLM 徒手编新闻）。"""
    zh = not str(lang or "zh").lower().startswith("en")
    if zh:
        return (
            "【新闻问句·无素材】对方在问最近的新闻，但你今天还没刷到可聊的"
            "——如实说今天还没顾上看手机/刷新闻，可以反问对方有没有看到什么"
            "有意思的。**绝不要**编造任何具体新闻事件。"
        )
    return (
        "[News question — no material] They're asking about recent news but "
        "you haven't caught up today. Say honestly you haven't checked your "
        "feed yet and ask what they've seen. NEVER invent any news event."
    )


def parse_topics_cfg(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读 ``companion.daily_topics``，返回归一化配置（全有默认值，永不抛）。

    ``blocklist`` 输出为「内置默认表 + 配置追加」的合并表；``feeds`` 显式
    置空列表 = 尊重运营选择（不回落默认）。默认 ``enabled: false``。
    """
    cfg = config if isinstance(config, dict) else {}
    comp = cfg.get("companion") if isinstance(cfg.get("companion"), dict) else {}
    dt = comp.get("daily_topics") if isinstance(comp, dict) else None
    if not isinstance(dt, dict):
        dt = {}

    def _f(key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(dt.get(key, default))
        except (TypeError, ValueError):
            v = float(default)
        return max(lo, min(hi, v))

    feeds_raw = dt.get("feeds")
    if isinstance(feeds_raw, (list, tuple)):
        feeds = [str(u).strip() for u in feeds_raw if str(u or "").strip()]
    else:
        feeds = list(DEFAULT_FEEDS)
    extra_block = dt.get("blocklist")
    if isinstance(extra_block, (list, tuple)):
        merged_block = list(DEFAULT_BLOCKLIST) + [
            str(w).strip() for w in extra_block if str(w or "").strip()]
    else:
        merged_block = list(DEFAULT_BLOCKLIST)
    kind_map = dt.get("feed_kinds")
    # region_feeds（实施55「人设所属国家的社会新闻」）：{区域键: [feed url]}。
    # 键＝人设 id（人设级本土源，如 lin_xiaoyu=哈尔滨）或国家码（CA/US/ES…，
    # 按人设居住地 country 命中）；值清洗成非空 url 列表，坏形态整键丢弃。
    region_raw = dt.get("region_feeds")
    region_feeds: Dict[str, List[str]] = {}
    if isinstance(region_raw, dict):
        for rk, urls in region_raw.items():
            key = re.sub(r"[^A-Za-z0-9_\-]", "", str(rk or "").strip())
            if not key or not isinstance(urls, (list, tuple)):
                continue
            clean = [str(u).strip() for u in urls if str(u or "").strip()]
            if clean:
                region_feeds[key] = clean
    # 实施84 P2 内容策略键（2026-08-29 老板拍板）：
    # exclude_sports 默认 **true**（产品级决定：不做球队赛事类分享）；
    # prefer_kinds 默认 娱乐/社会新闻靠前；user_region 默认关（新子系统惯例）。
    prefer_raw = dt.get("prefer_kinds")
    if isinstance(prefer_raw, (list, tuple)):
        prefer_kinds = [str(w).strip().lower() for w in prefer_raw
                        if str(w or "").strip()]
    else:
        prefer_kinds = ["entertainment", "news"]
    ur = dt.get("user_region") if isinstance(dt.get("user_region"), dict) else {}
    return {
        "enabled": bool(dt.get("enabled", False)),
        "feeds": feeds,
        "refresh_hours": _f("refresh_hours", 6, 0.1, 168),
        "max_items": int(_f("max_items", 8, 1, 50)),
        "pick_k": int(_f("pick_k", 3, 1, 10)),
        "cooldown_hours": _f("cooldown_hours", 4, 0, 168),
        "offer_percent": _f("offer_percent", 15, 0, 100),
        "blocklist": merged_block,
        "cache_path": str(dt.get("cache_path") or DEFAULT_CACHE_PATH),
        "feed_kinds": dict(kind_map) if isinstance(kind_map, dict) else {},
        "region_feeds": region_feeds,
        "exclude_sports": bool(dt.get("exclude_sports", True)),
        "prefer_kinds": prefer_kinds,
        "user_region_enabled": bool(ur.get("enabled", False)),
    }


# ── 按人设分源（实施55，2026-08-22「本土/人设所属国家社会新闻」）───────────────
def region_key_for(persona: Any, region_feeds: Optional[Dict[str, Any]]) -> str:
    """人设 → region_feeds 里的区域键：人设 id 直配优先 → 居住地国家码 →
    ""（用全局池）。纯函数，绝不抛。

    国家码来自 ``persona_location.resolve_place_with_fallback``（与人设钟同一
    事实源）——哈尔滨人设命中 "CN"、温哥华人设命中 "CA"；无居住地/无对应键
    回落全局 feeds（旧行为）。
    """
    feeds = region_feeds if isinstance(region_feeds, dict) else {}
    if not feeds:
        return ""
    try:
        p = persona if isinstance(persona, dict) else {}
        pid = str(p.get("id") or "").strip()
        if pid and pid in feeds:
            return pid
        from src.companion.persona_location import resolve_place_with_fallback
        place = resolve_place_with_fallback(p)
        if place is not None:
            cc = str(getattr(place, "country", "") or "").strip().upper()
            if cc and cc in feeds:
                return cc
    except Exception:
        pass
    return ""


def cfg_for_region(tcfg: Dict[str, Any], region_key: str) -> Dict[str, Any]:
    """把归一化配置克隆成某区域视图：feeds 换成区域源、缓存文件按键分家
    （``daily_topics_cache.<key>.json``，各区域独立刷新/独立 in_flight）。
    空键/无此键 → 原配置（全局池，零行为变化）。纯函数。
    """
    key = re.sub(r"[^A-Za-z0-9_\-]", "", str(region_key or "").strip())
    base = tcfg if isinstance(tcfg, dict) else {}
    feeds = (base.get("region_feeds") or {}).get(key) if key else None
    if not feeds:
        return dict(base)
    out = dict(base)
    out["feeds"] = list(feeds)
    p = Path(str(base.get("cache_path") or DEFAULT_CACHE_PATH))
    out["cache_path"] = str(p.with_name(f"{p.stem}.{key}{p.suffix}"))
    return out


# ── 按用户国别分源（实施84 P2，2026-08-29 老板拍板「以用户所在城市和国家为准」）──
def google_news_feed_url(country_code: str, lang: str = "zh") -> str:
    """按国家码 + 用户语言自动构造 Google News RSS 源 URL；非法输入返回 ""。

    中文用户 → 该国的简中版（如 ``gl=MY&ceid=MY:zh-Hans``）；其余语言用
    「语言-国家」版式（如 ``hl=en-MY&gl=MY&ceid=MY:en``）。Google 对不存在的
    版式组合会就近回落或返回空 feed——空 feed 走既有 fail-open 链（保留旧
    缓存 → 选题时回落全局池），绝不阻塞。纯函数。
    """
    cc = re.sub(r"[^A-Za-z]", "", str(country_code or "")).upper()
    if len(cc) != 2:
        return ""
    low = str(lang or "").strip().lower()
    if low.startswith("zh"):
        return (f"https://news.google.com/rss?hl=zh-CN&gl={cc}"
                f"&ceid={cc}:zh-Hans")
    m = re.match(r"^([a-z]{2})", low)
    l2 = m.group(1) if m else "en"
    return f"https://news.google.com/rss?hl={l2}-{cc}&gl={cc}&ceid={cc}:{l2}"


def cfg_for_user_region(
    tcfg: Dict[str, Any], country_code: str, lang: str = "zh",
) -> Optional[Dict[str, Any]]:
    """把归一化配置克隆成「**用户**所在国家」视图；不适用返回 None（调用方
    回落人设分源/全局池）。纯函数。

    优先级：① 运营显式配置的 ``region_feeds[国家码]``（人工选的源永远比
    自动构造的可信）→ ② ``user_region.enabled`` 时自动构造该国 Google News
    源（缓存文件按 ``u-<CC>`` 分家，各国独立刷新）。与 ``cfg_for_region``
    的关系：那是人设居住地分源（实施55），本函数是用户侧分源（实施84）——
    消费方先试本函数，None 再走人设分源，「用户在哪」优先于「人设在哪」。
    """
    base = tcfg if isinstance(tcfg, dict) else {}
    cc = re.sub(r"[^A-Za-z]", "", str(country_code or "")).upper()
    if len(cc) != 2:
        return None
    if cc in (base.get("region_feeds") or {}):
        return cfg_for_region(base, cc)
    if not base.get("user_region_enabled"):
        return None
    url = google_news_feed_url(cc, lang)
    if not url:
        return None
    out = dict(base)
    out["feeds"] = [url]
    p = Path(str(base.get("cache_path") or DEFAULT_CACHE_PATH))
    out["cache_path"] = str(p.with_name(f"{p.stem}.u-{cc}{p.suffix}"))
    return out


# ── last_offer 进程内账本（bounded）───────────────────────────────────────────
def last_offered(convo_key: str) -> float:
    """该会话上次附话题素材的时间戳；没有记录返回 0。"""
    with _OFFER_LOCK:
        try:
            return float(_OFFER_TS.get(str(convo_key or "")) or 0.0)
        except (TypeError, ValueError):
            return 0.0


def note_offered(convo_key: str, ts: Optional[float] = None) -> None:
    """记录该会话本轮附过话题素材（超上限裁掉最旧一半）。"""
    key = str(convo_key or "")
    if not key:
        return
    with _OFFER_LOCK:
        if len(_OFFER_TS) >= _OFFER_CAP and key not in _OFFER_TS:
            for old in sorted(_OFFER_TS, key=_OFFER_TS.get)[:_OFFER_CAP // 2]:
                _OFFER_TS.pop(old, None)
        _OFFER_TS[key] = float(ts if ts is not None else time.time())


__all__ = [
    "DEFAULT_BLOCKLIST",
    "DEFAULT_CACHE_PATH",
    "DEFAULT_FEEDS",
    "HEAVY_VETO_WORDS",
    "SMALLTALK_TOPIC_WORDS",
    "SPORTS_EVENT_WORDS",
    "build_no_topics_hint",
    "build_topics_hint",
    "cfg_for_region",
    "cfg_for_user_region",
    "feed_kind",
    "fetch_feed",
    "google_news_feed_url",
    "is_news_question",
    "is_sports_event_topic",
    "last_offered",
    "note_offered",
    "parse_topics_cfg",
    "pick_topics_for",
    "read_topics_cache",
    "refresh_if_stale",
    "refresh_now",
    "region_key_for",
    "sanitize_topics",
    "should_offer_topics",
    "smalltalk_topic",
    "smalltalk_verdict",
]
