"""今日话题包门禁（2026-08-02）。

不变量：
- ``fetch_feed`` 用注入的 fake urlopen 解析 RSS/Atom（绝不真联网），异常返 []；
- ``sanitize_topics``：HTML 剥离、屏蔽词剔除（灾难/政治人物）、标题相似度
  去重、时效过滤、时间新到旧；
- ``refresh_if_stale``：缓存 TTL 内不刷、过期才刷、抓取全败保留旧缓存、
  失败后短窗内不重试；缓存路径一律经 ``cache_path`` 注入 tmp_path，
  **绝不写仓库 config/**；
- ``pick_topics_for``：兴趣词命中优先 + crc32 当日确定性轮换，summary 截 80；
  ``variety_key``（联系人键）加盐＝不同联系人分散到整个话题池、同联系人
  当日恒定；省略＝旧键格式零行为变化（2026-08-04 霍尔木兹群发事故修复）；
- ``smalltalk_topic``：主动开场轻话题 allowlist——事故当天两台生产机缓存
  里的真实标题当金标（军政财法灾全 False、体育文娱生活全 True）；
- ``should_offer_topics``：触发词无视冷却恒 True；冷却期拦概率路径；
  概率路径 crc32 确定性可复算；
- ``build_topics_hint``：空列表返 ""，zh/en 双版；含反编造钉子；
- ``parse_topics_cfg``：全默认值（enabled 默认 False），blocklist=默认+追加。
"""
import io
import json
import time
from email.utils import formatdate
from zlib import crc32

from src.companion.daily_topics import (
    DEFAULT_BLOCKLIST,
    DEFAULT_CACHE_PATH,
    DEFAULT_FEEDS,
    build_topics_hint,
    feed_kind,
    fetch_feed,
    last_offered,
    note_offered,
    parse_topics_cfg,
    pick_topics_for,
    read_topics_cache,
    refresh_if_stale,
    refresh_now,
    sanitize_topics,
    should_offer_topics,
    smalltalk_topic,
    smalltalk_verdict,
)

NOW = 1785600000.0  # 固定「当前时间」，配合相对 pubDate 保证时效判定稳定


def _rss(now: float = NOW) -> bytes:
    def _d(hours_ago: float) -> str:
        return formatdate(now - hours_ago * 3600, usegmt=True)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>本地咖啡节这周末开幕</title>
  <description>&lt;b&gt;超过 50 家咖啡摊&lt;/b&gt;集中亮相，还有手冲体验区</description>
  <link>https://example.com/coffee</link><pubDate>{_d(2)}</pubDate></item>
<item><title>城市夜跑活动报名开启</title>
  <description>周五晚滨江步道，5 公里慢跑</description>
  <link>https://example.com/run</link><pubDate>{_d(5)}</pubDate></item>
<item><title>某地地震已造成人员伤亡</title>
  <description>灾情还在统计</description>
  <link>https://example.com/quake</link><pubDate>{_d(1)}</pubDate></item>
<item><title>特朗普发表最新讲话</title>
  <description>政治新闻</description>
  <link>https://example.com/politics</link><pubDate>{_d(1)}</pubDate></item>
<item><title>抹茶新品上市 全网热议</title>
  <description>新口味</description>
  <link>https://example.com/matcha1</link><pubDate>{_d(3)}</pubDate></item>
<item><title>抹茶新品上市全网热议!</title>
  <description>重复报道</description>
  <link>https://example.com/matcha2</link><pubDate>{_d(4)}</pubDate></item>
<item><title>一百小时前的旧闻</title>
  <description>early</description>
  <link>https://example.com/old</link><pubDate>{_d(100)}</pubDate></item>
</channel></rss>"""
    return xml.encode("utf-8")


def _fake_urlopen(payload: bytes, calls=None):
    def _open(url, timeout):
        if calls is not None:
            calls.append(url)
        return io.BytesIO(payload)
    return _open


# ── fetch_feed ──────────────────────────────────────────────────────────────
def test_fetch_feed_parses_rss_with_fake_urlopen():
    items = fetch_feed("http://fake/rss", urlopen_fn=_fake_urlopen(_rss()))
    assert len(items) == 7
    first = items[0]
    assert first["title"] == "本地咖啡节这周末开幕"
    assert first["link"] == "https://example.com/coffee"
    assert first["published_ts"] > 0
    assert "咖啡摊" in first["summary"]


def test_fetch_feed_parses_atom():
    atom = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>a</title>
<entry><title>Atom entry one</title><summary>hello world</summary>
<link href="https://example.com/a1"/>
<updated>2026-08-01T08:00:00Z</updated></entry></feed>"""
    items = fetch_feed("http://fake/atom", urlopen_fn=_fake_urlopen(atom))
    assert len(items) == 1
    assert items[0]["title"] == "Atom entry one"
    assert items[0]["link"] == "https://example.com/a1"
    assert items[0]["published_ts"] > 0


def test_fetch_feed_any_error_returns_empty():
    def _boom(url, timeout):
        raise OSError("network down")
    assert fetch_feed("http://fake/rss", urlopen_fn=_boom) == []
    bad = _fake_urlopen(b"this is not xml at all")
    assert fetch_feed("http://fake/rss", urlopen_fn=bad) == []


# ── sanitize_topics ─────────────────────────────────────────────────────────
def test_sanitize_blocklist_dedup_age_and_order():
    items = fetch_feed("http://fake/rss", urlopen_fn=_fake_urlopen(_rss()))
    topics = sanitize_topics(items, max_age_hours=48, now=NOW)
    titles = [t["title"] for t in topics]
    assert "某地地震已造成人员伤亡" not in titles      # 屏蔽词：地震/伤亡
    assert "特朗普发表最新讲话" not in titles          # 屏蔽词：政治人物
    assert "一百小时前的旧闻" not in titles            # 超 48h
    assert titles.count("抹茶新品上市 全网热议") == 1  # 近似标题去重
    assert "抹茶新品上市全网热议!" not in titles
    # HTML 剥离
    coffee = next(t for t in topics if "咖啡节" in t["title"])
    assert "<b>" not in coffee["summary"] and "咖啡摊" in coffee["summary"]
    # 新到旧
    ts = [t["published_ts"] for t in topics]
    assert ts == sorted(ts, reverse=True)


def test_sanitize_extra_blockword_and_latin_word_boundary():
    items = [
        {"title": "麻将大赛来了", "summary": "", "link": "", "published_ts": NOW},
        {"title": "Deadline extended for festival", "summary": "",
         "link": "", "published_ts": NOW},
    ]
    block = list(DEFAULT_BLOCKLIST) + ["麻将"]
    topics = sanitize_topics(items, blocklist=block, now=NOW)
    titles = [t["title"] for t in topics]
    assert "麻将大赛来了" not in titles
    # "dead" 是默认屏蔽词，但拉丁词按词边界匹配 → deadline 不误伤
    assert "Deadline extended for festival" in titles


# ── 缓存 TTL / 刷新 ─────────────────────────────────────────────────────────
def test_refresh_if_stale_ttl_and_write(tmp_path):
    cache = str(tmp_path / "topics_cache.json")
    calls: list = []
    cfg = {"enabled": True, "feeds": ["http://fake/rss"], "refresh_hours": 6,
           "max_items": 8, "cache_path": cache}
    fake = _fake_urlopen(_rss(), calls)
    # 无缓存 → 刷新（同步跑便于断言）
    assert refresh_if_stale(cfg, now=NOW, urlopen_fn=fake,
                            cache_path=cache, background=False) is True
    data = read_topics_cache(cache)
    assert data["fetched_ts"] == NOW
    assert len(data["topics"]) >= 2
    assert len(calls) == 1
    # TTL 内 → 不刷
    assert refresh_if_stale(cfg, now=NOW + 60, urlopen_fn=fake,
                            cache_path=cache, background=False) is False
    assert len(calls) == 1
    # 过期 → 再刷
    assert refresh_if_stale(cfg, now=NOW + 7 * 3600, urlopen_fn=fake,
                            cache_path=cache, background=False) is True
    assert len(calls) == 2
    assert read_topics_cache(cache)["fetched_ts"] == NOW + 7 * 3600


def test_refresh_all_fail_keeps_old_cache_and_throttles(tmp_path):
    cache = str(tmp_path / "topics_cache.json")
    old = {"fetched_ts": NOW - 24 * 3600,
           "topics": [{"title": "旧话题", "summary": "", "link": "",
                       "published_ts": NOW - 24 * 3600}]}
    (tmp_path / "topics_cache.json").write_text(
        json.dumps(old, ensure_ascii=False), encoding="utf-8")

    def _boom(url, timeout):
        raise OSError("down")
    cfg = {"enabled": True, "feeds": ["http://fake/rss"], "refresh_hours": 6,
           "max_items": 8, "cache_path": cache}
    assert refresh_if_stale(cfg, now=NOW, urlopen_fn=_boom,
                            cache_path=cache, background=False) is True
    kept = read_topics_cache(cache)
    assert kept["topics"][0]["title"] == "旧话题"      # 全败保留旧缓存
    assert kept["fetched_ts"] == NOW - 24 * 3600
    # 失败后的短窗内不再重试（防每条消息都触发重抓）
    assert refresh_if_stale(cfg, now=NOW + 30, urlopen_fn=_boom,
                            cache_path=cache, background=False) is False


def test_refresh_disabled_or_no_feeds_noop(tmp_path):
    cache = str(tmp_path / "c.json")
    assert refresh_if_stale({"enabled": False, "feeds": ["http://x"]},
                            now=NOW, cache_path=cache,
                            background=False) is False
    assert refresh_if_stale({"enabled": True, "feeds": []},
                            now=NOW, cache_path=cache,
                            background=False) is False
    assert not (tmp_path / "c.json").exists()


# ── pick_topics_for ─────────────────────────────────────────────────────────
def _cache_fixture():
    return {"fetched_ts": NOW, "topics": [
        {"title": "本地咖啡节这周末开幕", "summary": "超过 50 家咖啡摊",
         "link": "", "published_ts": NOW - 3600},
        {"title": "城市夜跑活动报名开启", "summary": "5 公里慢跑",
         "link": "", "published_ts": NOW - 7200},
        {"title": "新展览亮相美术馆", "summary": "当代艺术", "link": "",
         "published_ts": NOW - 9000},
        {"title": "超长摘要的话题", "summary": "长" * 200, "link": "",
         "published_ts": NOW - 9500},
    ]}


def test_pick_taste_match_first_and_deterministic():
    cache = _cache_fixture()
    got = pick_topics_for(["咖啡"], k=2, now=NOW, cache=cache)
    assert got[0]["title"] == "本地咖啡节这周末开幕"   # 兴趣命中优先
    assert len(got) == 2
    # 当日确定性：同一天再取结果一致
    again = pick_topics_for(["咖啡"], k=2, now=NOW + 120, cache=cache)
    assert [t["title"] for t in got] == [t["title"] for t in again]


def test_pick_summary_truncated_and_k_cap():
    got = pick_topics_for([], k=10, now=NOW, cache=_cache_fixture())
    assert len(got) == 4
    long_one = next(t for t in got if t["title"] == "超长摘要的话题")
    assert len(long_one["summary"]) <= 80


def test_pick_empty_cache():
    assert pick_topics_for(["咖啡"], cache={}) == []
    assert pick_topics_for(["咖啡"], cache={"topics": []}) == []


# ── pick_topics_for：variety_key 按联系人分散（2026-08-04 群发事故修复）─────
def test_pick_variety_key_spreads_and_stable_per_contact():
    cache = _cache_fixture()
    picks = {
        pick_topics_for([], k=1, now=NOW, cache=cache,
                        variety_key=f"contact:{i}")[0]["title"]
        for i in range(16)
    }
    assert len(picks) >= 2                       # 不再全员同题
    a = pick_topics_for([], k=1, now=NOW, cache=cache,
                        variety_key="contact:7")
    b = pick_topics_for([], k=1, now=NOW + 3600, cache=cache,
                        variety_key="contact:7")
    assert a == b                                # 同联系人当日恒定


def test_pick_variety_key_omitted_keeps_legacy_rotation():
    """省略 variety_key ＝ 旧键格式（日期#标题）——既有调用方零行为变化。"""
    cache = _cache_fixture()
    day = time.strftime("%Y-%m-%d", time.localtime(NOW))
    legacy = sorted(
        (t["title"] for t in cache["topics"]),
        key=lambda title: crc32(f"{day}#{title}".encode("utf-8")))
    got = [t["title"] for t in pick_topics_for([], k=4, now=NOW, cache=cache)]
    assert got == legacy


def test_pick_variety_key_taste_match_still_first():
    got = pick_topics_for(["咖啡"], k=2, now=NOW, cache=_cache_fixture(),
                          variety_key="c:x")
    assert got[0]["title"] == "本地咖啡节这周末开幕"   # 兴趣命中仍最高优先


# ── smalltalk_topic（轻话题 allowlist，主动开场专用）────────────────────────
def test_smalltalk_allowlist_on_incident_goldens():
    """2026-08-04 群发事故金标：两台生产机当天缓存里的真实标题。

    硬新闻（军政财法灾）全部曾穿过 DEFAULT_BLOCKLIST——allowlist 的存在
    理由就是它们；轻话题必须全部合格（否则主动开场会饿死）。
    """
    heavy = [
        {"title": "美伊官员称伊朗与阿曼接近达成霍尔木兹海峡航行协议 - 同花顺",
         "summary": "伊朗称与阿曼谈判接近完成 美军向外界征集军事行动方案"},
        {"title": "山东省人大法制委员会原副主任委员唐福泉被开除党籍和公职",
         "summary": ""},
        {"title": "才让太辞去青海省副省长职务（附简历）", "summary": ""},
        {"title": "日元“跌跌不休”，美国为何罕见出手干预？",
         "summary": "关于美国罕见出手干预日元汇率"},
        {"title": "商品日报（8月4日）：硅铁“逆势”拉涨超3% 集运欧线午盘“跳水”超7%",
         "summary": ""},
        {"title": "英国运用推演与3D建模技术升级海军扫雷战力", "summary": ""},
        {"title": "国务院成立重庆彭水汉葭街道“7·17”特别重大山体崩塌灾害调查评估组",
         "summary": ""},
        {"title": "從「勸阻」到限制出境，中國如何重新定義出入境自由？",
         "summary": ""},
        # 媒体品牌噪声金标：聚合摘要里的「手机新浪网」曾点亮轻词「手机」，
        # 让边境管控新闻穿过 allowlist——重词否决（边界/管控/移民/危机）先行
        {"title": "欧盟向西班牙提供援助 加强休达外部边界管控 - 第一财经",
         "summary": "国际观察｜休达危机折射欧盟移民政策困境 人民网 西班牙飞地"
                    "移民危机在欧盟内部引发激烈争吵 新浪新闻_手机新浪网"},
        {"title": "危地马拉富埃戈火山喷发 政府疏散多个村庄", "summary": ""},
        {"title": "贝森特：美伊有望4日或5日达成协议 恢复荷莫兹海峡自由通行 - RFI",
         "summary": ""},
    ]
    for t in heavy:
        assert smalltalk_topic(t) is False, t["title"]
    light = [
        {"title": "首次纳入广东省运会竞技体育组 霹雳舞赛事在茂名收官",
         "summary": ""},
        {"title": "宁夏原州“村BA”落幕 乡土赛事点燃乡村文体活力",
         "summary": "一个篮球，拍出文明乡风"},
        {"title": "WTA1000多伦多站：张帅终结对普丁塞娃七连败，王欣瑜遭逆转出局",
         "summary": "网球｜加拿大国家银行公开赛：张帅晋级女单第二轮"},
        {"title": "2026年中国电影暑期档票房突破75亿元 《功夫女足》暂领跑",
         "summary": ""},
        {"title": "湖南祁阳青年反哺家乡 支教创业激活乡村新活力", "summary": ""},
        {"title": "AI短剧女主方桃子接美瞳广告，网友质疑", "summary": ""},
        {"title": "本地咖啡节这周末开幕", "summary": "超过 50 家咖啡摊"},
    ]
    for t in light:
        assert smalltalk_topic(t) is True, t["title"]


def test_smalltalk_latin_word_boundary_and_bad_shapes():
    assert smalltalk_topic(
        {"title": "New coffee festival opens downtown", "summary": ""}) is True
    assert smalltalk_topic({"title": "no such thing here", "summary": ""}) is False
    assert smalltalk_topic(None) is False
    assert smalltalk_topic({}) is False


def test_smalltalk_verdict_explains_hits():
    """可解释判定（周审 CLI 的读数口）：ok 与 smalltalk_topic 恒一致 + 命中词可见。"""
    heavy = {"title": "欧盟向西班牙提供援助 加强休达外部边界管控",
             "summary": "移民危机在欧盟内部引发激烈争吵 新浪新闻_手机新浪网"}
    v = smalltalk_verdict(heavy)
    assert v["ok"] is False
    assert "边界" in v["veto_hits"] and "移民" in v["veto_hits"]
    light = {"title": "WTA1000多伦多站：张帅晋级",
             "summary": "网球｜加拿大国家银行公开赛"}
    v2 = smalltalk_verdict(light)
    assert v2["ok"] is True and v2["veto_hits"] == []
    assert "网球" in v2["light_hits"]
    # 单一事实源：布尔口径与 verdict.ok 永远一致
    for t in (heavy, light, {}, None):
        assert smalltalk_topic(t) is smalltalk_verdict(t)["ok"]


# ── should_offer_topics ─────────────────────────────────────────────────────
def test_trigger_words_bypass_cooldown():
    recent = NOW - 60  # 刚给过素材，仍在冷却
    assert should_offer_topics("好无聊啊", recent, now=NOW) is True
    assert should_offer_topics("最近有什么新鲜事吗", recent, now=NOW) is True
    assert should_offer_topics("what's up?", recent, now=NOW) is True
    assert should_offer_topics("在干嘛呢", recent, now=NOW) is True


def test_cooldown_blocks_probabilistic_path():
    # 非触发词 + 冷却期内 → 恒 False（无论掷签结果）
    for text in ("嗯嗯", "今天好热", "刚下班", "ok", "哈哈哈"):
        assert should_offer_topics(text, NOW - 3600, now=NOW,
                                   cooldown_hours=4) is False


def test_probabilistic_path_matches_crc_formula_and_is_deterministic():
    lt = time.localtime(NOW)
    for text in ("嗯嗯", "今天好热", "刚下班回家", "看了个电影", "早安"):
        key = f"{time.strftime('%Y-%m-%d', lt)}#{lt.tm_hour}#{text}"
        expected = crc32(key.encode("utf-8")) % 100 < 15
        got = should_offer_topics(text, 0, now=NOW)
        assert got is expected
        assert should_offer_topics(text, 0, now=NOW) is got  # 同输入恒定


# ── build_topics_hint ───────────────────────────────────────────────────────
def test_build_hint_zh_and_en():
    topics = [{"title": "本地咖啡节这周末开幕", "summary": "超过 50 家咖啡摊"},
              {"title": "城市夜跑活动报名开启", "summary": ""}]
    zh = build_topics_hint(topics, lang="zh")
    assert "【今日新鲜事·可选素材】" in zh
    assert "1) 本地咖啡节这周末开幕——超过 50 家咖啡摊" in zh
    assert "2) 城市夜跑活动报名开启" in zh
    assert "别像播报新闻" in zh and "别硬转话题" in zh
    en = build_topics_hint(topics, lang="en")
    assert "fresh topics" in en and "news anchor" in en


def test_build_hint_empty():
    assert build_topics_hint([]) == ""
    assert build_topics_hint(None) == ""
    assert build_topics_hint([{"summary": "无标题"}]) == ""


def test_build_hint_forbids_fabrication():
    """反编造钉子：素材只有一句标题，细节不知道、别编、被追问就承认没细看。"""
    topics = [{"title": "本地咖啡节这周末开幕", "summary": ""}]
    zh = build_topics_hint(topics, lang="zh")
    assert "不要编造" in zh and "没细看" in zh
    en = build_topics_hint(topics, lang="en")
    assert "never invent" in en and "skimmed" in en


# ── parse_topics_cfg ────────────────────────────────────────────────────────
def test_topics_cfg_defaults():
    cfg = parse_topics_cfg({})
    assert cfg["enabled"] is False       # 新子系统默认关（本仓惯例）
    assert cfg["feeds"] == list(DEFAULT_FEEDS)
    assert cfg["refresh_hours"] == 6
    assert cfg["max_items"] == 8
    assert cfg["pick_k"] == 3
    assert cfg["cooldown_hours"] == 4
    assert cfg["cache_path"] == DEFAULT_CACHE_PATH
    assert list(cfg["blocklist"][:len(DEFAULT_BLOCKLIST)]) == list(DEFAULT_BLOCKLIST)


def test_topics_cfg_overrides_and_blocklist_append():
    cfg = parse_topics_cfg({"companion": {"daily_topics": {
        "enabled": True, "feeds": [], "refresh_hours": 12,
        "blocklist": ["麻将"], "pick_k": 5,
    }}})
    assert cfg["enabled"] is True
    assert cfg["feeds"] == []            # 显式空列表受尊重（不回落默认）
    assert cfg["refresh_hours"] == 12
    assert cfg["pick_k"] == 5
    assert "麻将" in cfg["blocklist"] and "地震" in cfg["blocklist"]
    # 非 dict 输入不抛
    assert parse_topics_cfg(None)["enabled"] is False
    assert parse_topics_cfg({"companion": "oops"})["pick_k"] == 3


# ── last_offer 账本 ─────────────────────────────────────────────────────────
def test_offer_ledger_roundtrip():
    key = "test:daily_topics:ledger"
    assert last_offered(key) == 0.0
    note_offered(key, ts=NOW)
    assert last_offered(key) == NOW
    note_offered("", ts=NOW)             # 空 key 不落账
    assert last_offered("") == 0.0


# ── 新闻直球问句（2026-08-12「问新闻答没关注」实录）────────────────────────
def test_is_news_question_positive():
    from src.companion.daily_topics import is_news_question
    for t in ("最近有什么新闻吗", "今天有什么大事", "世界上发生了什么事",
              "有啥新鲜事", "看热搜没", "any news today?",
              "what's happening in the world"):
        assert is_news_question(t), t


def test_is_news_question_negative():
    from src.companion.daily_topics import is_news_question
    for t in ("今天好累啊", "你吃饭了吗", "我想你了", "", None):
        assert not is_news_question(t), t


def test_direct_ask_hint_is_imperative():
    """直球问 → 强指令：必须正面作答、禁「没什么新闻」、含反编造钉子。"""
    topics = [{"title": "本地咖啡节这周末开幕", "summary": "s", "source": ""}]
    hint = build_topics_hint(topics, direct_ask=True)
    assert "正在问你" in hint and "不要" in hint
    assert "编造" in hint                       # 反编造钉子仍在
    passive = build_topics_hint(topics)
    assert hint != passive                       # 两档口径确实不同
    en = build_topics_hint(topics, lang="en", direct_ask=True)
    assert "asking you" in en.lower() and "never invent" in en


def test_no_topics_hint_honest_fallback():
    from src.companion.daily_topics import build_no_topics_hint
    zh = build_no_topics_hint("zh")
    assert "编造" in zh and "没" in zh
    en = build_no_topics_hint("en")
    assert "invent" in en.lower()
    # 空列表 build_topics_hint 仍返 ""（direct_ask 也不例外——兜底走 no_topics）
    assert build_topics_hint([], direct_ask=True) == ""


# ── kind 标签（2026-08-16 P2：类别长在源上，供分类分发/观测）─────────────────
def test_feed_kind_url_patterns_and_override():
    ent = ("https://news.google.com/rss/headlines/section/topic/"
           "ENTERTAINMENT?hl=zh-CN")
    spt = "https://news.google.com/rss/headlines/section/topic/SPORTS?x=1"
    assert feed_kind(ent) == "entertainment"
    assert feed_kind(spt) == "sports"
    assert feed_kind("https://feeds.bbci.co.uk/zhongwen/simp/rss.xml") == "news"
    assert feed_kind("") == "news"
    # 配置覆写（子串命中）优先于内置 URL 模式
    assert feed_kind("https://example.com/mysports.xml",
                     {"mysports": "sports"}) == "sports"
    assert feed_kind(ent, {"topic/entertainment": "showbiz"}) == "showbiz"


def test_refresh_attaches_kind_and_pick_carries_it(tmp_path):
    cache = str(tmp_path / "topics_cache.json")
    cfg = {"enabled": True,
           "feeds": ["https://x/rss/headlines/section/topic/SPORTS?a=1"],
           "max_items": 8, "cache_path": cache}
    assert refresh_now(cfg, now=NOW, urlopen_fn=_fake_urlopen(_rss()),
                       cache_path=cache) is True
    data = read_topics_cache(cache)
    assert data["topics"], "缓存应有条目"
    assert {t.get("kind") for t in data["topics"]} == {"sports"}
    picked = pick_topics_for(None, k=2, now=NOW, cache=data)
    assert picked and all(p["kind"] == "sports" for p in picked)


def test_sanitize_kind_defaults_news_for_legacy_items():
    # 旧缓存/未标注条目（无 kind 字段）→ 回落 "news"，向后兼容
    items = [{"title": "本地咖啡节这周末开幕", "summary": "", "link": "",
              "published_ts": NOW}]
    topics = sanitize_topics(items, now=NOW)
    assert topics[0]["kind"] == "news"


def test_parse_cfg_feed_kinds_passthrough():
    cfg = parse_topics_cfg({"companion": {"daily_topics": {
        "feed_kinds": {"mysports": "sports"}}}})
    assert cfg["feed_kinds"] == {"mysports": "sports"}
    assert parse_topics_cfg({})["feed_kinds"] == {}


# ── 2026-08-16 四源试点词表补丁（召回与 veto 两端，全部实测形态）──────────────
def test_smalltalk_recall_entertainment_verticals():
    # 垂直娱乐源实测 no-hit 白白饿死主动开场的形态：主演/开播/定档/上映
    for t in (
        {"title": "段奕宏、邢佳栋再联手，《藏锋》主演阵容官宣", "summary": ""},
        {"title": "《披荆斩棘2026》定档下周开播", "summary": ""},
        {"title": "新片全国上映首日口碑出炉", "summary": ""},
    ):
        assert smalltalk_topic(t) is True, t["title"]


def test_smalltalk_veto_ipo_and_party_column():
    # 首刷实测穿透旧词表的两类：硬财经（IPO/停复牌）与党建栏目稿
    for t in (
        {"title": "频准激光：将于8月18日在上交所科创板上市", "summary": ""},
        {"title": "纳指ETF嘉实因大幅溢价将于8月17日开市起停牌", "summary": ""},
        {"title": "切实担负起管党治党政治责任", "summary": ""},
        {"title": "市委巡视组进驻本地文旅集团", "summary": ""},
    ):
        assert smalltalk_topic(t) is False, t["title"]


# ── 实施55（2026-08-22）：注入频率可配 + 按人设分源 ────────────────────────────

def test_parse_cfg_offer_percent_default_and_clamp():
    assert parse_topics_cfg({})["offer_percent"] == 15          # 缺省=旧行为
    assert parse_topics_cfg({"companion": {"daily_topics": {
        "offer_percent": 35}}})["offer_percent"] == 35
    assert parse_topics_cfg({"companion": {"daily_topics": {
        "offer_percent": 999}}})["offer_percent"] == 100        # 夹界
    assert parse_topics_cfg({"companion": {"daily_topics": {
        "offer_percent": "bad"}}})["offer_percent"] == 15       # 脏值回默认


def test_should_offer_topics_percent_bounds():
    # 0%：概率路径永不放行（触发词仍无视一切恒 True）
    assert should_offer_topics(
        "随便聊聊今天", 0.0, now=NOW, offer_percent=0) is False
    assert should_offer_topics(
        "有什么新鲜事", 0.0, now=NOW, offer_percent=0) is True
    # 100%：冷却外恒放行；冷却内仍拦（percent 不越过冷却语义）
    assert should_offer_topics(
        "随便聊聊今天", 0.0, now=NOW, offer_percent=100) is True
    assert should_offer_topics(
        "随便聊聊今天", NOW - 600, now=NOW,
        cooldown_hours=4, offer_percent=100) is False


def test_parse_cfg_region_feeds_sanitized():
    cfg = parse_topics_cfg({"companion": {"daily_topics": {"region_feeds": {
        "lin_xiaoyu": ["https://a/rss", "  ", None],
        "CA": ["https://ca/rss"],
        "bad key!!": ["https://x/rss"],     # 键消毒后仍非法字符剥掉→"badkey"
        "empty": [],                        # 空列表整键丢弃
        "wrong": "not-a-list",              # 坏形态整键丢弃
    }}}})
    rf = cfg["region_feeds"]
    assert rf["lin_xiaoyu"] == ["https://a/rss"]
    assert rf["CA"] == ["https://ca/rss"]
    assert "empty" not in rf and "wrong" not in rf
    assert rf.get("badkey") == ["https://x/rss"]
    assert parse_topics_cfg({})["region_feeds"] == {}


def test_region_key_for_pid_over_country_over_global():
    from src.companion.daily_topics import region_key_for
    feeds = {"lin_xiaoyu": ["u1"], "CA": ["u2"]}
    # 人设 id 直配最优先
    assert region_key_for({"id": "lin_xiaoyu", "location": "vancouver"},
                          feeds) == "lin_xiaoyu"
    # 无 id 配 → 居住地国家码
    assert region_key_for({"id": "lin_jiaxin", "location": "vancouver"},
                          feeds) == "CA"
    # 国家无配 / 无居住地 / 空表 → ""（全局池）
    assert region_key_for({"id": "x", "location": "shanghai"}, feeds) == ""
    assert region_key_for({"id": "x"}, feeds) == ""
    assert region_key_for({"id": "lin_xiaoyu"}, {}) == ""
    assert region_key_for(None, feeds) == ""


def test_cfg_for_region_swaps_feeds_and_cache_path(tmp_path):
    from src.companion.daily_topics import cfg_for_region
    base = parse_topics_cfg({"companion": {"daily_topics": {
        "feeds": ["https://global/rss"],
        "cache_path": str(tmp_path / "topics_cache.json"),
        "region_feeds": {"CA": ["https://ca/rss"]},
    }}})
    r = cfg_for_region(base, "CA")
    assert r["feeds"] == ["https://ca/rss"]
    assert r["cache_path"].endswith("topics_cache.CA.json")
    assert r["blocklist"] == base["blocklist"]      # 屏蔽词全局同一份
    # 空键/未知键 → 原配置（全局池零行为变化）
    assert cfg_for_region(base, "")["cache_path"] == base["cache_path"]
    assert cfg_for_region(base, "XX")["feeds"] == base["feeds"]
