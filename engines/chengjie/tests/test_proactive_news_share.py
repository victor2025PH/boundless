# -*- coding: utf-8 -*-
"""主动触达「今日新鲜事」开场门禁（news_share，2026-08-03）。

锁五条不变量：
  1. 双开关旁路：``companion.daily_topics.enabled`` 与
     ``companion.daily_topics.proactive.enabled``（默认 False）都开才活。
  2. 无货回落：缓存空/缺文件/超过 24h 没刷新（旧闻不冒充「今天看到的」）
     → {} → 升级链自然落到下一级，零行为变化。
  3. 升级链顺序＝生活分享 → 新鲜事 → 天气：life_share 原样放行（生活线
     优先不变，连人设词解析都不触发）；gentle_checkin / weather_hook 才升级
     （news 优先于天气、让位于生活分享）；follow_up/ask_* 等一概不动。
  4. 72h 每会话频控：真发落账后窗口内不再用 news_share，过窗恢复。
  5. mode_gate 富开场集合含 news_share（含 collect_mode_gate 端到端聚合）。
  6. 轻话题硬闸 + 按联系人分散 + 时效软偏好（2026-08-04 霍尔木兹群发
     事故修复）：军政财新闻绝不开场（全池硬新闻 → 回落）；轮换盐含
     联系人键（多联系人分散到话题池，同联系人当日恒定）；今天内条目
     优先、全旧放宽；directive 带反编造钉子。
  7. 同日用量均匀化 + 72h 账本落盘（P2）：真发落账后选题只在今日用量
     最少的子池轮换（顺序发送≈轮询，9 发×3 题=3+3+3）、跨日清零；
     频控账本经 set_news_ledger_path 镜像 JSON——桌面打包版每日多次
     重启不再把 72h 窗清成「距上次重启」；未注入路径绝不落盘。
"""
from __future__ import annotations

import json
import time

from src.companion.proactive_mode_gate import (
    CHECKIN_MODE,
    RICH_MODES,
    collect_mode_gate,
    parse_mode_gate_cfg,
)
from src.companion.proactive_topic import (
    NEWS_MODE,
    NEWS_REPEAT_WINDOW_HOURS,
    _news_opener,
    maybe_upgrade_to_news,
    news_opener_allowed,
    note_news_opener,
)

NOW = 1785600000.0  # 固定「当前时间」，缓存新鲜度/频控判定稳定


def _cfg(cache_path, *, enabled=True, proactive=True):
    """最小配置：feeds 显式空列表（parse 尊重空=绝不抓网），缓存指 tmp。"""
    return {"companion": {"daily_topics": {
        "enabled": enabled,
        "feeds": [],
        "cache_path": str(cache_path),
        "proactive": {"enabled": proactive},
    }}}


def _stock(cache_path, *, fetched_ts=NOW, topics=None):
    rows = topics if topics is not None else [
        {"title": "本地咖啡节这周末开幕", "summary": "超过 50 家咖啡摊",
         "link": "", "published_ts": NOW - 3600},
        {"title": "城市夜跑活动报名开启", "summary": "5 公里慢跑",
         "link": "", "published_ts": NOW - 7200},
    ]
    cache_path.write_text(
        json.dumps({"fetched_ts": fetched_ts, "topics": rows},
                   ensure_ascii=False), encoding="utf-8")


# ── 1. 双开关旁路 ────────────────────────────────────────────────────────

def test_gated_on_both_switches(tmp_path):
    cache = tmp_path / "topics_cache.json"
    _stock(cache)
    # daily_topics 总开关关 → 不活（即便 proactive 子键开着）
    assert _news_opener(
        _cfg(cache, enabled=False), "c:sw:1", now=NOW) == {}
    # proactive 子键缺省/关 → 不活（反应式已上线不等于主动开闸）
    off = _cfg(cache)
    del off["companion"]["daily_topics"]["proactive"]
    assert _news_opener(off, "c:sw:2", now=NOW) == {}
    assert _news_opener(
        _cfg(cache, proactive=False), "c:sw:3", now=NOW) == {}
    # 双开 → 活
    op = _news_opener(_cfg(cache), "c:sw:4", now=NOW)
    assert op.get("mode") == NEWS_MODE and op.get("directive")


# ── 2. 无货回落 ──────────────────────────────────────────────────────────

def test_no_stock_returns_empty(tmp_path):
    cache = tmp_path / "topics_cache.json"
    # 缓存文件不存在
    assert _news_opener(_cfg(cache), "c:ns:1", now=NOW) == {}
    # 缓存存在但 topics 为空
    _stock(cache, topics=[])
    assert _news_opener(_cfg(cache), "c:ns:2", now=NOW) == {}


def test_stale_cache_is_no_stock(tmp_path):
    """超过 24h 没刷出来的缓存不能当「今天看到的」讲（诚实口径）。"""
    cache = tmp_path / "topics_cache.json"
    _stock(cache, fetched_ts=NOW - 25 * 3600)
    assert _news_opener(_cfg(cache), "c:st:1", now=NOW) == {}
    # 23h 内仍算新鲜
    _stock(cache, fetched_ts=NOW - 23 * 3600)
    assert _news_opener(
        _cfg(cache), "c:st:2", now=NOW).get("mode") == NEWS_MODE


def test_crisis_gate_block(tmp_path):
    cache = tmp_path / "topics_cache.json"
    _stock(cache)
    assert _news_opener(_cfg(cache), "c:gb:1", gate="block", now=NOW) == {}


# ── 3. 素材与指令 ────────────────────────────────────────────────────────

def test_directive_content_and_taste_priority(tmp_path):
    cache = tmp_path / "topics_cache.json"
    _stock(cache)
    # 人设词命中 → 优先选中该条（与反应式 pick_topics_for 同语义）
    op = _news_opener(
        _cfg(cache), "c:dc:1", now=NOW, persona_words=["夜跑"])
    assert op["mode"] == NEWS_MODE
    assert op["fact"] == "城市夜跑活动报名开启"
    d = op["directive"]
    assert "城市夜跑活动报名开启" in d and "5 公里慢跑" in d
    # 任务口径：看法/感受 + 轻巧问题；禁播报/禁链接/禁多件；反编造钉子
    assert "看法" in d and "问题" in d
    assert "播报" in d and "链接" in d and "一次讲多件" in d
    assert "不要编造" in d and "没细看" in d
    assert op["context_facts"] == []
    # 词取不到（空列表）→ 当日确定性轮换：**同一联系人**同日恒定
    # （轮换盐含联系人键——不同联系人分散到话题池，见第 7 节）
    a = _news_opener(_cfg(cache), "c:dc:2", now=NOW, persona_words=[])
    b = _news_opener(_cfg(cache), "c:dc:2", now=NOW + 120, persona_words=[])
    assert a["fact"] == b["fact"]


# ── 4. 72h 频控 ──────────────────────────────────────────────────────────

def test_repeat_window_pure_ledger():
    key = "test:news:ledger:pure"
    assert news_opener_allowed(key, now=NOW) is True
    note_news_opener(key, ts=NOW)
    assert news_opener_allowed(key, now=NOW + 71 * 3600) is False
    assert news_opener_allowed(
        key, now=NOW + (NEWS_REPEAT_WINDOW_HOURS + 1) * 3600) is True
    # 空 key 不落账、恒放行
    note_news_opener("", ts=NOW)
    assert news_opener_allowed("", now=NOW) is True


def test_repeat_window_blocks_opener(tmp_path):
    cache = tmp_path / "topics_cache.json"
    _stock(cache)
    key = "test:news:ledger:opener"
    assert _news_opener(
        _cfg(cache), key, now=NOW).get("mode") == NEWS_MODE
    note_news_opener(key, ts=NOW)  # 真发落账（_on_teaser_sent 语义）
    assert _news_opener(_cfg(cache), key, now=NOW + 3600) == {}
    # 过窗恢复（缓存按生产节奏同步刷新，否则会先撞 24h 新鲜度闸）
    after = NOW + (NEWS_REPEAT_WINDOW_HOURS + 1) * 3600
    _stock(cache, fetched_ts=after - 600)
    assert _news_opener(
        _cfg(cache), key, now=after).get("mode") == NEWS_MODE


# ── 5. 升级链顺序 ────────────────────────────────────────────────────────

def test_life_share_passes_through_untouched(tmp_path):
    """生活线优先不变：life_share 原样放行，连人设词解析都不触发。"""
    cache = tmp_path / "topics_cache.json"
    _stock(cache)
    called = []
    op = {"mode": "life_share", "fact": "路过花店", "directive": "分享",
          "silent_hours": 30.0}
    out = maybe_upgrade_to_news(
        op, _cfg(cache), "c:up:life", now=NOW,
        persona_words_fn=lambda: called.append(1) or [])
    assert out is op
    assert called == []  # lazy：不升级绝不做人设解析


def test_checkin_and_weather_upgrade_to_news(tmp_path):
    cache = tmp_path / "topics_cache.json"
    _stock(cache)
    ck = {"mode": "gentle_checkin", "fact": "", "directive": "问候",
          "silent_hours": 48.5, "gap_bucket": "few_days"}
    out = maybe_upgrade_to_news(ck, _cfg(cache), "c:up:ck", now=NOW)
    assert out["mode"] == NEWS_MODE
    assert out["silent_hours"] == 48.5          # 透传：prompt 框定按真实沉默
    assert out["gap_bucket"] == "few_days"
    # news 优先于天气：weather_hook 也会被换成 news_share
    wx = {"mode": "weather_hook", "fact": "暴雨", "directive": "说说暴雨",
          "silent_hours": 30.0, "gap_bucket": ""}
    out2 = maybe_upgrade_to_news(wx, _cfg(cache), "c:up:wx", now=NOW)
    assert out2["mode"] == NEWS_MODE


def test_no_stock_falls_back_to_original(tmp_path):
    """无货回落：升级失败返回原 opener（weather 继续当 weather）。"""
    cache = tmp_path / "topics_cache.json"   # 不写缓存 = 无货
    wx = {"mode": "weather_hook", "fact": "暴雨", "directive": "说说暴雨"}
    assert maybe_upgrade_to_news(
        wx, _cfg(cache), "c:up:nofall", now=NOW) is wx
    ck = {"mode": "gentle_checkin", "fact": "", "directive": "问候"}
    assert maybe_upgrade_to_news(
        ck, _cfg(cache), "c:up:nofall2", now=NOW) is ck


def test_other_modes_never_touched(tmp_path):
    cache = tmp_path / "topics_cache.json"
    _stock(cache)
    for mode in ("follow_up", "ask_birthday", "story_invite",
                 "ritual_morning", ""):
        op = {"mode": mode, "fact": "", "directive": "x"}
        assert maybe_upgrade_to_news(
            op, _cfg(cache), f"c:up:other:{mode}", now=NOW) is op


# ── 6. mode_gate 富开场集合 ──────────────────────────────────────────────

def test_rich_modes_contains_news_share():
    assert NEWS_MODE in RICH_MODES
    assert CHECKIN_MODE not in RICH_MODES
    # 生活线/天气仍在（别把老成员挤掉）
    for m in ("follow_up", "life_share", "weather_hook"):
        assert m in RICH_MODES


# ── 7. 轻话题闸 + 按联系人分散 + 时效偏好（2026-08-04 霍尔木兹群发修复）──

def test_hormuz_incident_hard_news_never_opens(tmp_path):
    """事故金标（2026-08-04）：全池都是军政财新闻 → {} 回落天气/问候。

    当天 198 打包机真实缓存里的四条硬新闻全部穿过了 DEFAULT_BLOCKLIST，
    其中霍尔木兹一条在 73 分钟内被群发给 9 个客户（含 BotFather）——
    allowlist 硬闸的存在理由。
    """
    cache = tmp_path / "topics_cache.json"
    _stock(cache, topics=[
        {"title": "美伊官员称伊朗与阿曼接近达成霍尔木兹海峡航行协议 - 同花顺",
         "summary": "伊朗称与阿曼谈判接近完成 美军向外界征集军事行动方案",
         "link": "", "published_ts": NOW - 3600},
        {"title": "山东省人大法制委员会原副主任委员唐福泉被开除党籍和公职",
         "summary": "", "link": "", "published_ts": NOW - 3600},
        {"title": "日元“跌跌不休”，美国为何罕见出手干预？",
         "summary": "关于美国罕见出手干预日元汇率",
         "link": "", "published_ts": NOW - 3600},
        {"title": "英国运用推演与3D建模技术升级海军扫雷战力",
         "summary": "", "link": "", "published_ts": NOW - 3600},
    ])
    assert _news_opener(_cfg(cache), "c:hz:1", now=NOW) == {}
    # 升级链回落：checkin 保持原样（不因新闻无货而空转）
    ck = {"mode": "gentle_checkin", "fact": "", "directive": "问候"}
    assert maybe_upgrade_to_news(ck, _cfg(cache), "c:hz:2", now=NOW) is ck


_LIGHT_WTA = {
    "title": "WTA1000多伦多站：张帅终结对普丁塞娃七连败，王欣瑜遭逆转出局",
    "summary": "网球｜加拿大国家银行公开赛：张帅晋级女单第二轮",
    "link": "", "published_ts": NOW - 3600}
_LIGHT_BBALL = {
    "title": "宁夏原州“村BA”落幕 乡土赛事点燃乡村文体活力",
    "summary": "一个篮球，拍出文明乡风", "link": "", "published_ts": NOW - 7200}


def test_mixed_pool_only_light_topics_open(tmp_path):
    """混合池只出轻话题：军政条目对任何联系人都不可见。"""
    cache = tmp_path / "topics_cache.json"
    _stock(cache, topics=[
        {"title": "美伊官员称伊朗与阿曼接近达成霍尔木兹海峡航行协议 - 同花顺",
         "summary": "伊朗称与阿曼谈判接近完成",
         "link": "", "published_ts": NOW - 1800},
        _LIGHT_WTA,
        {"title": "才让太辞去青海省副省长职务（附简历）",
         "summary": "", "link": "", "published_ts": NOW - 1800},
        _LIGHT_BBALL,
    ])
    light_facts = {_LIGHT_WTA["title"][:80], _LIGHT_BBALL["title"][:80]}
    for i in range(12):
        op = _news_opener(_cfg(cache), f"c:mx:{i}", now=NOW)
        assert op.get("mode") == NEWS_MODE
        assert op["fact"] in light_facts, op["fact"]


def test_contacts_spread_across_topic_pool(tmp_path):
    """按联系人加盐：多联系人不再全员同题（修「9 人收到同一条霍尔木兹」）。"""
    cache = tmp_path / "topics_cache.json"
    _stock(cache, topics=[_LIGHT_WTA, _LIGHT_BBALL])
    got = {
        _news_opener(_cfg(cache), f"c:sp:{i}", now=NOW)["fact"]
        for i in range(16)
    }
    assert len(got) >= 2


def test_stale_topics_soft_fallback_still_opens(tmp_path):
    """条目全旧（>24h）但缓存新鲜 → 放宽仍出货（时效是偏好不是硬闸）。"""
    cache = tmp_path / "topics_cache.json"
    old = dict(_LIGHT_WTA)
    old["published_ts"] = NOW - 40 * 3600
    _stock(cache, topics=[old])
    assert _news_opener(
        _cfg(cache), "c:sf:1", now=NOW).get("mode") == NEWS_MODE


def test_fresh_topic_preferred_over_stale(tmp_path):
    """有今天内的条目时，隔夜旧条目不参与（对所有联系人成立）。"""
    cache = tmp_path / "topics_cache.json"
    old = dict(_LIGHT_WTA)
    old["published_ts"] = NOW - 40 * 3600
    _stock(cache, topics=[old, _LIGHT_BBALL])
    for i in range(8):
        op = _news_opener(_cfg(cache), f"c:fp:{i}", now=NOW)
        assert op["fact"] == _LIGHT_BBALL["title"][:80]


# ── 8. 同日用量均匀化 + 72h 账本落盘（P2 2026-08-04）─────────────────────

_LIGHT_COFFEE = {"title": "本地咖啡节这周末开幕", "summary": "超过 50 家咖啡摊",
                 "link": "", "published_ts": NOW - 3600}


def _reset_topic_usage():
    import src.companion.proactive_topic as pt
    with pt._NEWS_TOPIC_USE_LOCK:
        pt._NEWS_TOPIC_USE.clear()
        pt._NEWS_TOPIC_USE_DAY = ""


def test_topic_usage_evens_out_distribution(tmp_path):
    """用量最少优先：顺序真发 9 次 × 3 话题 → 恰好 3+3+3（回放曾 4+4+1）。"""
    from collections import Counter

    from src.companion.proactive_topic import note_news_topic_used
    cache = tmp_path / "topics_cache.json"
    _stock(cache, topics=[_LIGHT_WTA, _LIGHT_BBALL, _LIGHT_COFFEE])
    _reset_topic_usage()
    try:
        dist: Counter = Counter()
        for i in range(9):
            op = _news_opener(_cfg(cache), f"c:use:{i}", now=NOW)
            assert op.get("mode") == NEWS_MODE
            dist[op["fact"]] += 1
            # 模拟 _on_teaser_sent 真发落账（fact=标题前 80 字口径）
            note_news_topic_used(op["fact"], now=NOW)
        assert len(dist) == 3
        assert max(dist.values()) == 3
    finally:
        _reset_topic_usage()


def test_topic_usage_day_rollover_resets():
    from src.companion.proactive_topic import (
        news_topic_use_count,
        note_news_topic_used,
    )
    _reset_topic_usage()
    try:
        note_news_topic_used("某个话题标题", now=NOW)
        assert news_topic_use_count("某个话题标题", now=NOW) == 1
        # 次日同话题归零（跨日整体清空）
        assert news_topic_use_count("某个话题标题", now=NOW + 86400) == 0
    finally:
        _reset_topic_usage()


def test_news_ledger_persists_across_restart(tmp_path):
    """72h 频控账本落盘：清空进程内（模拟桌面版重启）后窗口仍拦得住。"""
    import src.companion.proactive_topic as pt
    ledger = tmp_path / "companion_news_opener.json"
    k1, k2 = "test:news:persist:1", "test:news:persist:2"
    try:
        pt.set_news_ledger_path(str(ledger))
        note_news_opener(k1, ts=NOW)
        assert ledger.exists()
        # 模拟进程重启：内存清空 + 重新注入路径（触发懒加载）
        with pt._NEWS_OPENER_LOCK:
            pt._NEWS_OPENER_TS.clear()
        pt.set_news_ledger_path(None)
        pt.set_news_ledger_path(str(ledger))
        assert news_opener_allowed(k1, now=NOW + 3600) is False   # 窗口内仍拦
        assert news_opener_allowed(
            k1, now=NOW + (NEWS_REPEAT_WINDOW_HOURS + 1) * 3600) is True
        # 落账时瘦身：超过 2×窗口的旧条目被清出文件
        note_news_opener(k2, ts=NOW + 200 * 3600)
        data = json.loads(ledger.read_text(encoding="utf-8"))
        assert k2 in data and k1 not in data
    finally:
        pt.set_news_ledger_path(None)
        with pt._NEWS_OPENER_LOCK:
            pt._NEWS_OPENER_TS.pop(k1, None)
            pt._NEWS_OPENER_TS.pop(k2, None)


def test_news_ledger_no_path_no_file(tmp_path):
    """未注入路径（单测/未接线）＝纯进程内，绝不落盘。

    注：conftest 的 autouse 隔离夹具会往 tmp_path 放自己的库文件，
    只断言「没有本账本的文件出现」而非目录为空。
    """
    import src.companion.proactive_topic as pt
    assert pt._NEWS_LEDGER_PATH is None
    note_news_opener("test:news:nofile", ts=NOW)
    hits = [p for p in tmp_path.rglob("*") if "news_opener" in p.name]
    assert hits == []


def test_collect_mode_gate_counts_news_in_rich_arm(tmp_path):
    """端到端：outreach note=news_share 计入富开场臂（与 P5 _seed 同口径）。"""
    from src.inbox.models import InboxConversation, InboxMessage
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    d1 = now - 86400

    def _one(cid, note, ts, replied):
        store.record_outreach(cid, batch_id="proactive_topic:text",
                              note=note, ts=ts)
        if replied:
            store.ingest_batch(
                InboxConversation(conversation_id=cid, platform="telegram",
                                  chat_key=cid.split(":")[-1]),
                [InboxMessage(conversation_id=cid, platform_msg_id=f"r{cid}",
                              direction="in", text="回", ts=ts + 300)])

    for i in range(40):
        _one(f"tg:a:c{i}", "gentle_checkin", d1 + i, i < 4)
    for i in range(20):
        _one(f"tg:a:nw{i}", "news_share", d1 + 7200 + i, i < 10)
    snap = collect_mode_gate(
        store, parse_mode_gate_cfg({"mode_gate": {"enabled": True}}), now=now)
    assert snap["rich"]["sent"] == 20
    assert snap["rich"]["modes"] == {"news_share": 20}
    assert abs(snap["rich"]["rate"] - 0.5) < 0.01
    assert snap["reason"] == "ok" and snap["skip_prob"] > 0
