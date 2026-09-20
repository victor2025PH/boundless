# -*- coding: utf-8 -*-
"""人设内容排查器门禁（2026-08-03「删除不干净」事故链）。

实锤背景：林小雨的「猫」被创建/丰富期反规范化写进 6+ 个字段（hobbies /
background / tastes / selfie_scenes / life_arc / family…），运营在表单删掉
「爱好」后 AI 照提不误。本排查器是「这个词还在哪」的单一入口——门禁重点：
① 全字段递归召回（漏报=运营以为删干净了，比误报糟得多）；
② 通道/可编辑标注不许说反（把场景池标成 inert = 误导运营「不用管」）；
③ 运行时层只读扫描软失败（库缺席绝不 500）。
"""
import sqlite3
import time

from src.utils.persona_content_scan import (
    scan_bio_chunks,
    scan_episodic,
    scan_history,
    scan_outbound,
    scan_profile_fields,
    split_scan_terms,
    suggest_related_terms,
)

# 与生产实锤同构的样例（zhiliao lin_xiaoyu 2026-08-03 现场的猫分布）
PERSONA = {
    "id": "lin_test",
    "name": "林小雨",
    "background": "自由撰稿人，养了一只橘猫毛豆",
    "context": {
        "hobbies": ["撸学校后门的流浪猫", "煮茶"],
        "family": {"pet": "橘猫毛豆"},
        "schedule": {"night_tone": "在家撸猫看剧"},
    },
    "tastes": {"likes": ["猫", "热茶"], "opinions": ["养猫要负责"]},
    # ⚠ 字面扫描的已知边界（首跑实证）：生产原文是「捡到一只小三花在宿舍楼下」
    # ——语义上是猫、字面无「猫」字，纯 LIKE 扫不到。同义扩展是 P2 增量
    # （复用 persona_entity_alias 的别名哲学）；样例这里显式带「猫」字钉住
    # 「life_arc 层能被召回」这个通道断言本身。
    "selfie_scenes": ["cat cafe with a sleepy cat on the table"],
    "life_arc": {"beats": ["捡到一只小三花猫在宿舍楼下"]},
    "voice_profile": {"reference_audio_path": "voice_refs/cat_probe.wav"},
}


def _by_path(q: str):
    return {h["path"]: h for h in scan_profile_fields(PERSONA, q)}


def test_scan_recalls_all_denormalized_spots():
    """事故核心断言：一个词的全部藏身处必须一个不漏地被点名。"""
    paths = set(_by_path("猫").keys())
    for expect in (
        "background",
        "context.hobbies[0]",
        "context.family.pet",
        "context.schedule.night_tone",
        "tastes.likes[0]",
        "tastes.opinions[0]",
        "life_arc.beats[0]",
    ):
        assert expect in paths, f"漏报 {expect}"


def test_scan_latin_case_insensitive():
    hits = _by_path("CAT")
    assert "selfie_scenes[0]" in hits


def test_channel_labels_not_lying():
    """通道标注错向 = 误导运营：场景池是「在干嘛」的答案来源，绝不能标 inert。"""
    by = _by_path("猫")
    assert by["background"]["channel"] == "persona_prompt"
    assert by["context.family.pet"]["channel"] == "persona_prompt"
    assert by["context.schedule.night_tone"]["channel"] == "persona_prompt"
    assert by["life_arc.beats[0]"]["channel"] == "life_beat"
    latin = _by_path("cat")
    assert latin["selfie_scenes[0]"]["channel"] == "scene_state"
    assert latin["voice_profile.reference_audio_path"]["channel"] == "inert"


def test_ui_editable_flags():
    """「表单可改 vs 需高级字段」是运营下一步动作的指路牌，方向不能反。"""
    by = _by_path("猫")
    assert by["context.hobbies[0]"]["ui_editable"] is True
    assert by["tastes.likes[0]"]["ui_editable"] is True
    assert by["context.family.pet"]["ui_editable"] is False
    assert by["context.schedule.night_tone"]["ui_editable"] is False
    assert by["life_arc.beats[0]"]["ui_editable"] is False


def test_snippet_windows_long_text():
    long_bg = {"background": "废话" * 200 + "中间藏着一只猫" + "废话" * 200}
    hits = scan_profile_fields(long_bg, "猫")
    assert len(hits) == 1
    assert "猫" in hits[0]["text"]
    assert len(hits[0]["text"]) < 120  # 摘窗而非全文回传


def test_empty_inputs():
    assert scan_profile_fields(PERSONA, "") == []
    assert scan_profile_fields(None, "猫") == []
    assert scan_profile_fields(PERSONA, "不存在的词汇") == []


# ── 多词与同义建议（P2 期：字面扫描两大盲区的收口——语义同义 + 跨语言）─────

def test_split_scan_terms():
    assert split_scan_terms("猫，猫咖, cat、boba；tea") == \
        ["猫", "猫咖", "cat", "boba", "tea"]
    assert split_scan_terms(" 猫 ,猫, CAT ,cat ") == ["猫", "CAT"]   # 大小写去重保序
    assert split_scan_terms("a,b,c,d,e,f,g") == ["a", "b", "c", "d", "e"]  # 上限 5
    assert split_scan_terms("") == []
    assert split_scan_terms("，，、") == []


def test_suggest_related_terms_cross_language():
    """核心断言：中文「猫」必须建议出英文 cat——英文场景池漏扫的正解。"""
    sugg = suggest_related_terms(["猫"])
    assert "cat" in sugg
    assert "猫咖" in sugg
    assert "猫" not in sugg                        # 已含词不重复建议
    # 组内任一成员都能反查整组（双向）
    assert "猫" in suggest_related_terms(["kitty"])
    # 无关词零建议、空入参零建议
    assert suggest_related_terms(["量子力学"]) == []
    assert suggest_related_terms([]) == []


# ── 运行时层（tmp sqlite；生产同构 schema）──────────────────────────────────

def test_suggest_related_terms_extra_groups():
    """运营自维护同义词组（personas.content_scan.synonym_groups）：
    业务黑话/人设专名（毛豆→猫）内置表进不去，config 钩子必须生效且防手滑。"""
    from src.utils.persona_content_scan import suggest_related_terms
    extra = [["毛豆", "猫", "豆豆"], "不是列表的组该被跳过", ["单元素组跳过"]]
    out = suggest_related_terms(["毛豆"], extra)
    assert "猫" in out and "豆豆" in out
    # 运营组优先出现在建议里（先于内置组联想）
    assert out.index("猫") <= 2
    # 内置组仍工作（extra 为 None=旧行为）
    assert "猫咪" in suggest_related_terms(["猫"], None)
    # 全非法形状 → 不炸，退回内置组
    assert suggest_related_terms(["猫"], "garbage") == suggest_related_terms(["猫"])


def test_scan_bio_chunks(tmp_path):
    db = tmp_path / "persona_bio.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE persona_bio_chunks"
              " (persona_id TEXT, idx INTEGER, text TEXT, embedding TEXT)")
    c.executemany(
        "INSERT INTO persona_bio_chunks VALUES (?,?,?,NULL)",
        [("lin_test", 0, "她养了一只猫叫毛豆"),
         ("lin_test", 1, "喜欢煮茶"),
         ("other_pid", 0, "猫猫猫")])
    c.commit()
    c.close()
    out = scan_bio_chunks(db, "lin_test", "猫")
    assert out["available"] is True
    assert out["count"] == 1          # 只算本人设，别把别家的算进来
    assert "猫" in out["samples"][0]["text"]
    # 库缺席 = 软失败，不抛
    missing = scan_bio_chunks(tmp_path / "nope.db", "lin_test", "猫")
    assert missing["available"] is False


def test_scan_episodic_and_history(tmp_path):
    db = tmp_path / "bot.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE episodic_memory"
              " (id INTEGER PRIMARY KEY, user_id TEXT, content TEXT)")
    c.execute("INSERT INTO episodic_memory (user_id, content)"
              " VALUES ('u1', 'TA喜欢你的猫')")
    c.execute("INSERT INTO episodic_memory (user_id, content)"
              " VALUES ('u2', '无关事实')")
    c.execute("CREATE TABLE user_context"
              " (user_id TEXT, data TEXT, updated_at REAL)")
    c.execute("INSERT INTO user_context VALUES"
              " ('u1', '{\"history\": \"聊到猫\"}', 1.0)")
    c.commit()
    c.close()
    epi = scan_episodic(db, "猫")
    assert epi["available"] and epi["count"] == 1
    assert epi["samples"][0]["memory_key"] == "u1"
    hist = scan_history(db, "猫")
    assert hist["available"] and hist["count"] == 1
    # 历史层刻意只回 key 不摘原文（隐私），断言形状钉住这个约定
    assert list(hist["samples"][0].keys()) == ["memory_key"]


def test_scan_outbound_window_and_direction(tmp_path):
    db = tmp_path / "inbox.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE messages (message_id TEXT, conversation_id TEXT,"
              " direction TEXT, text TEXT, ts REAL)")
    now = time.time()
    c.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        [("m1", "c1", "out", "我家猫今天很乖", now - 3600),
         ("m2", "c1", "in", "你的猫呢", now - 3600),        # 入站不算（症状=我们发出的）
         ("m3", "c1", "out", "很久以前的猫", now - 90 * 86400)])  # 窗口外
    c.commit()
    c.close()
    out = scan_outbound(db, "猫", days=14)
    assert out["available"] and out["count"] == 1
    assert out["samples"][0]["conversation_id"] == "c1"
