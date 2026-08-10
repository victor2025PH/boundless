# -*- coding: utf-8 -*-
"""出站近重复守卫纯函数门禁（2026-07-31，198 实锤三连发）。

金标取自客户机 inbox.db 实录：
  - 10:07:45 / 10:07:59 / 10:08:28 —— 同一条入站的三条同义改写全部发出
    （共同开头『Haha, that's true.』）；
  - 10:11:00 发出的提问在 10:12:07 被原样再发（客户已回答过）。
守卫语义：dup=原样重复/包含；similar=同义开头改写。两档都只用于「要求坐席确认」，
过短文本（『好的』『嗯嗯』）永不判——重复短语是正常聊天。
"""

from __future__ import annotations

from src.inbox.outbound_dup_guard import (
    dup_guard_metrics_snapshot,
    near_duplicate_of_recent,
    normalize_for_dup,
    record_dup_check,
)

NOW = 1_785_460_000.0


def _out(text, age_sec):
    return {"direction": "out", "text": text, "ts": NOW - age_sec}


def _in(text, age_sec):
    return {"direction": "in", "text": text, "ts": NOW - age_sec}


def test_exact_repeat_question_is_dup():
    """实录：把客户已答过的问题隔 67 秒原样再问 → dup。"""
    q = "What about you, do you usually cook or are you more used to ordering takeout?😄"
    rows = [_out(q, 67), _in("Take out is so expensive now", 30)]
    hit = near_duplicate_of_recent(q, rows, now=NOW)
    assert hit and hit["level"] == "dup"
    assert hit["age_sec"] >= 60


def test_contained_previous_question_is_dup():
    """旧问题被整体拼进新话里再发 → 仍是 dup（containment 判定）。"""
    q = "What about you, do you usually cook or are you more used to ordering takeout?"
    new = "Yeah eating out is pricey. " + q
    hit = near_duplicate_of_recent(new, [_out(q, 60)], now=NOW)
    assert hit and hit["level"] == "dup"


def test_paraphrase_variants_are_similar():
    """实录三连发：同义改写共享开头『Haha, that's true.』→ similar（要求确认）。"""
    first = "Haha, that's true. Making new friends when you're bored sounds like a good start, doesn't it?"
    second = "Haha, that's true. No rush, just let things happen naturally."
    hit = near_duplicate_of_recent(second, [_out(first, 14)], now=NOW)
    assert hit and hit["level"] == "similar"


def test_normal_conversation_flow_not_flagged():
    """正常追问/新话题不误伤。"""
    rows = [_out("Nice, 35 is a great age. I'm 41 myself, kind of an old man now haha 😄", 40)]
    assert near_duplicate_of_recent(
        "Haha, you really know how to make someone smile 😄", rows, now=NOW) is None


def test_short_texts_never_flagged():
    """『好的』『嗯嗯』这类口头禅重复是正常聊天，永不判。"""
    rows = [_out("好的", 5), _out("ok!", 5)]
    assert near_duplicate_of_recent("好的", rows, now=NOW) is None
    assert near_duplicate_of_recent("ok!", rows, now=NOW) is None


def test_window_expiry_and_direction_filter():
    q = "What about you, do you usually cook or are you more used to ordering takeout?"
    # 超出 180s 窗口 → 放行
    assert near_duplicate_of_recent(q, [_out(q, 300)], now=NOW) is None
    # 入站同文（客户自己的话）不参与比对
    assert near_duplicate_of_recent(q, [_in(q, 10)], now=NOW) is None


def test_defensive_on_bad_rows():
    q = "What about you, do you usually cook or are you more used to ordering takeout?"
    rows = [None, {"direction": "out"}, {"direction": "out", "text": q, "ts": "bad"},
            "junk", {"direction": "out", "text": q, "ts": NOW - 5}]
    hit = near_duplicate_of_recent(q, rows, now=NOW)
    assert hit and hit["level"] == "dup"      # 脏行跳过，好行仍命中
    assert near_duplicate_of_recent(q, None, now=NOW) is None


def test_normalize_strips_punct_emoji_space():
    a = normalize_for_dup("Haha, that's true!! 😄")
    b = normalize_for_dup("haha thats true")
    assert a == b


def test_metrics_counters():
    base = dup_guard_metrics_snapshot()
    record_dup_check("dup")
    record_dup_check("similar")
    record_dup_check("")
    record_dup_check("", forced=True)
    snap = dup_guard_metrics_snapshot()
    assert snap["checked"] == base["checked"] + 4
    assert snap["hit_dup"] == base["hit_dup"] + 1
    assert snap["hit_similar"] == base["hit_similar"] + 1
    assert snap["forced"] == base["forced"] + 1


# ── 2026-08-02 扩展：内容词重合判据（金标=本机 30 天生产语料实锤重复对）──────

def test_paraphrase_by_token_overlap_linda_case():
    """实锤（客户截图）：开头各异的同义改写，旧两档全 miss → token 重合抓住。"""
    first = ("But hey, you're chatting with me now, "
             "so it's not totally alone, right? 😊")
    second = "But hey, you're not really alone right now, are you? I'm here."
    hit = near_duplicate_of_recent(second, [_out(first, 21)], now=NOW)
    assert hit and hit["level"] == "similar"


def test_paraphrase_by_token_overlap_cjk_case():
    """实锤（WA 林佳欣）：自我介绍句整体复读 + 各接不同下文 → similar。"""
    first = "我叫林佳欣呀，佳是美好的佳，欣是欣赏的欣😊\n\n你呢，怎么称呼好？"
    second = "我叫林佳欣呀，佳是美好的佳，欣是欣赏的欣😊\n\n温哥华现在深夜快两三点啦，我刚热完夜宵。"
    hit = near_duplicate_of_recent(second, [_out(first, 25)], now=NOW)
    assert hit and hit["level"] == "similar"


def test_legit_followup_reask_not_flagged():
    """校准负样本：客户没回，换措辞再问一次称呼（char 0.49）→ 合法跟进不误伤。"""
    first = "哈哈，我叫顾嘉啦～你呢，怎么称呼？"
    second = "我叫顾嘉啦，无界科技的方案顾问。你还没告诉我怎么称呼你呢～"
    assert near_duplicate_of_recent(second, [_out(first, 30)], now=NOW) is None


def test_media_placeholder_rows_only_dup_level():
    """相册配文风格天然雷同（校准负样本 0.667/0.645）→ 占位行不参与 similar 档。"""
    first = "[图片] 今天在咖啡馆里，感觉自己像个小公主！"
    second = "[图片] 今天在咖啡馆偶遇了自己！"
    assert near_duplicate_of_recent(second, [_out(first, 30)], now=NOW) is None
    # 但逐字重发媒体占位仍是 dup（防重发同图配文）
    hit = near_duplicate_of_recent(first, [_out(first, 30)], now=NOW)
    assert hit and hit["level"] == "dup"


def test_registry_register_recent_unregister():
    from src.inbox.outbound_dup_guard import RecentOutboundRegistry
    reg = RecentOutboundRegistry(ttl_sec=100.0, max_per_conv=2)
    t1 = reg.register("c1", "第一条在途消息内容", now=NOW - 10)
    reg.register("c1", "第二条在途消息内容", now=NOW - 5)
    rows = reg.recent_rows("c1", now=NOW)
    assert [r["text"] for r in rows] == ["第一条在途消息内容", "第二条在途消息内容"]
    assert all(r["direction"] == "out" for r in rows)
    # 撤销第一条（投递失败场景）
    reg.unregister("c1", t1)
    assert [r["text"] for r in reg.recent_rows("c1", now=NOW)] == ["第二条在途消息内容"]
    # TTL 过期自动剪枝
    assert reg.recent_rows("c1", now=NOW + 200) == []
    # 条数上限：只留最近 max_per_conv 条
    for i in range(4):
        reg.register("c2", f"消息内容第{i}号", now=NOW + i)
    assert len(reg.recent_rows("c2", now=NOW + 4)) == 2
    # 空文本/空会话不登记
    assert reg.register("", "x", now=NOW) == 0
    assert reg.register("c3", "   ", now=NOW) == 0


def test_registry_rows_feed_guard():
    """登记表行与 store 行同形状，可直接并入守卫比对。"""
    from src.inbox.outbound_dup_guard import RecentOutboundRegistry
    reg = RecentOutboundRegistry()
    q = "What about you, do you usually cook or are you more used to ordering takeout?"
    reg.register("c1", q, now=NOW - 20)
    hit = near_duplicate_of_recent(q, reg.recent_rows("c1", now=NOW), now=NOW)
    assert hit and hit["level"] == "dup"


def test_resolve_guard_cfg_defaults_and_overrides():
    from src.inbox.outbound_dup_guard import resolve_guard_cfg
    cfg = resolve_guard_cfg({})
    assert cfg == {"enabled": False, "window_sec": 180.0, "block_similar": True}
    cfg2 = resolve_guard_cfg({"inbox": {"outbound_dup_guard": {
        "enabled": True, "window_sec": 300, "block_similar": False}}})
    assert cfg2["enabled"] and cfg2["window_sec"] == 300.0
    assert cfg2["block_similar"] is False
    assert resolve_guard_cfg(None)["enabled"] is False


def test_blocked_by_source_counter():
    base = dup_guard_metrics_snapshot().get(
        "blocked_by_source", {}).get("autosend", 0)
    record_dup_check("similar", source="autosend", blocked=True)
    record_dup_check("", source="autosend", blocked=False)  # 未命中不计拦截
    snap = dup_guard_metrics_snapshot()
    assert snap["blocked_by_source"]["autosend"] == base + 1
