# -*- coding: utf-8 -*-
"""「AI 味」量化门禁（活人感 P1-9/P1-10，2026-08-03）。

覆盖：开场词统计口径 / flavor_report 指标 / 语音镜像清洗 / AI 质疑保守词表
（正负样本，负样本全部来自真实语料形态）/ episode 回复率口径 / 验收目标判据
（含**探测器有效性自证**：喂 2026-08-03 生产实锤形态的坏语料必不达标）/
周报 CLI 端到端（种子 sqlite，零生产依赖）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from src.eval.ai_flavor_eval import (
    build_verdicts,
    detect_ai_suspicion,
    episode_reply_stats,
    flavor_report,
    merge_reply_stats,
    opener_of,
    target_checks,
    voice_mirror_text,
)

# ── 开场词口径 ────────────────────────────────────────────────────────────────


def test_opener_of_laugh_and_multi():
    assert opener_of("哈哈哈你还真执着啊") == "哈哈"      # 连字折叠 + 不要求分隔符
    assert opener_of("哈哈，被你看穿了") == "哈哈"
    assert opener_of("嘿嘿，我就知道") == "嘿嘿"
    assert opener_of("哎呀这话说得") == "哎呀"            # 多字话语标记不要求分隔符
    assert opener_of("其实我觉得还行") == "其实"
    assert opener_of("hahaha ok") == "haha"
    assert opener_of("「哇塞，真的假的」") == "哇塞"       # 引号剥除


def test_opener_of_single_needs_separator():
    assert opener_of("嘿，这回能听清了") == "嘿"
    assert opener_of("嘿你说什么") == ""                  # 单字无分隔符不算
    assert opener_of("啊？你说什么") == ""                # ？不是分隔符：真实反应不算
    assert opener_of("嗯，好呀") == "嗯"
    assert opener_of("正常一句话") == ""
    assert opener_of("") == ""


# ── flavor_report 指标 ───────────────────────────────────────────────────────


def test_flavor_report_rates():
    texts = [
        "哈哈今天真好",                    # laugh opener
        "嘿，在吗？",                      # interjection + question
        "正常说话。",
        "其实我觉得可以。你呢？",          # interjection + multi-sentence + question = essay
    ]
    rep = flavor_report(texts)
    assert rep["n"] == 4
    assert rep["laugh_opener_rate"] == 0.25
    assert rep["interjection_opener_rate"] == 0.75
    assert rep["question_rate"] == 0.5
    assert rep["question_end_rate"] == 0.5
    assert rep["emoji_rate"] == 0.0
    assert rep["multi_sentence_rate"] == 0.25
    assert rep["essay_rate"] == 0.25
    assert rep["top_openers"][0][0] in ("哈哈", "嘿", "其实")


def test_flavor_report_emoji_service_and_empty():
    rep = flavor_report(["收到，我看一下马上回您～ 😊", "", None])
    assert rep["n"] == 1
    assert rep["emoji_rate"] == 1.0
    assert rep["tilde_rate"] == 1.0
    assert rep["service_tone_rate"] == 1.0     # 「您」+客服短语
    empty = flavor_report([])
    assert empty["n"] == 0 and empty["len_avg"] == 0.0


def test_flavor_report_question_end_ignores_trailing_emoji():
    rep = flavor_report(["你吃饭了吗？😊"])
    assert rep["question_end_rate"] == 1.0


# ── 语音镜像清洗 ─────────────────────────────────────────────────────────────


def test_voice_mirror_text():
    assert voice_mirror_text("[语音] 哈哈好吧好吧") == "哈哈好吧好吧"
    assert voice_mirror_text("[voice] morning!") == "morning!"
    assert voice_mirror_text("早安呀") == "早安呀"
    assert voice_mirror_text("[语音]×3") == ""
    assert voice_mirror_text("[语音]") == ""


# ── AI 质疑检测（保守词表：正负样本双面）────────────────────────────────────


def test_suspicion_positives():
    assert detect_ai_suspicion("你是不是AI啊") == "direct"
    assert detect_ai_suspicion("你就是个机器人吧") == "direct"
    assert detect_ai_suspicion("跟我说实话，真人吗？") == "direct"
    assert detect_ai_suspicion("are you a bot?") == "direct"
    assert detect_ai_suspicion("You're an AI right") == "direct"
    assert detect_ai_suspicion("刚才那句一股机器味") == "flavor"
    assert detect_ai_suspicion("说话像个机器人一样") == "flavor"
    assert detect_ai_suspicion("sounds like a robot") == "flavor"


def test_suspicion_negatives_no_false_alarm():
    # 全部是真实语料形态：聊 AI 工具/话题 ≠ 质疑身份（误报会让运营不信这个数）
    for s in (
        "我今天用AI做了个图，你看看",
        "AI绘画现在好火啊",
        "机器学习难学吗",
        "chatgpt帮我写了作业",
        "店里报表都交给AI跑了",
        "哈哈你真逗",
        "机器坏了，正在修",
        "",
    ):
        assert detect_ai_suspicion(s) == "", s


# ── episode 回复率口径 ───────────────────────────────────────────────────────


def test_episode_burst_collapses_and_reply_window():
    t0 = 1_000_000.0
    rows = [
        (t0, "out", ""), (t0 + 60, "out", ""), (t0 + 120, "out", "voice"),
        (t0 + 3600, "in", ""),                       # 48h 内回复 → 该簇 replied
        (t0 + 90000, "out", ""),                     # 第二簇：无回复
    ]
    st = episode_reply_stats(rows)
    # 连发 3 条（含 1 条语音）＝一个 voice 簇；第二簇纯文字未回
    assert st["voice"] == {"episodes": 1, "replied": 1}
    assert st["text"] == {"episodes": 1, "replied": 0}


def test_episode_gap_splits_and_window_expiry():
    t0 = 1_000_000.0
    rows = [
        (t0, "out", ""), (t0 + 7200, "out", ""),     # 间隔 2h > 30min → 两个簇
        (t0 + 7200 + 49 * 3600, "in", ""),           # 49h 后才回 → 超 48h 窗不算
    ]
    st = episode_reply_stats(rows)
    assert st["text"] == {"episodes": 2, "replied": 0}


def test_episode_pending_window_excluded_with_now():
    t0 = 1_000_000.0
    rows = [(t0, "out", "")]
    # 回复窗未走完（now 距发送仅 1h）→ 不计分母，防「抢答未回」
    st = episode_reply_stats(rows, now=t0 + 3600)
    assert st["text"] == {"episodes": 0, "replied": 0}
    # 窗走完仍未回 → 正常计入未回
    st2 = episode_reply_stats(rows, now=t0 + 49 * 3600)
    assert st2["text"] == {"episodes": 1, "replied": 0}


def test_merge_reply_stats_rates():
    total = merge_reply_stats([
        {"voice": {"episodes": 2, "replied": 1}, "text": {"episodes": 0, "replied": 0}},
        {"voice": {"episodes": 2, "replied": 1}, "text": {"episodes": 5, "replied": 4}},
    ])
    assert total["voice"]["episodes"] == 4 and total["voice"]["reply_rate"] == 0.5
    assert total["text"]["reply_rate"] == 0.8


# ── 验收判据 + 探测器有效性自证 ──────────────────────────────────────────────


def _bad_corpus():
    """2026-08-03 生产实锤形态：哈哈开场 + 必反问 + 长作文。"""
    return [
        "哈哈，你还真执着啊😆 我现在气喘吁吁的，唱出来会吓到你。你晚饭吃了啥好吃的？",
        "哈哈被你看穿了😅 我昨晚夜班吃夜宵是真的。你别介意啊？",
        "哈哈，好啦，那我努力靠谱一点😄 我刚健身完。要不要来陪我聊会儿？",
        "嘿，刚看见票房超了。国漫实力不容小觑啊😂 你这大半夜的咋开始看新闻了呀？",
        "哎呀，这话说得我都不知咋回了……你到底想让我讲啥呢～",
    ]


def test_detector_validity_bad_corpus_flagged():
    """探测器有效性自证：坏语料必不达标（评测不是摆设）。"""
    overall = flavor_report(_bad_corpus())
    checks = target_checks(overall, flavor_report([]))
    by_name = {c["name"]: c for c in checks}
    assert not by_name["同质化开场占比"]["ok"]
    assert not by_name["消息含反问率"]["ok"]


def test_clean_corpus_passes_targets():
    # 健康分布：反问 3/10（带内——0 反问会被下限拦：完全不发问也不像在聊天）
    overall = flavor_report([
        "今天去了趟菜市场，买到超新鲜的虾",
        "刚下课，你到家没？",
        "那部剧你看到第几集了？",
        "周六有空吗？",
        "我妈今天炖了汤，香得很",
        "下午开会开到现在才结束，累瘫",
        "你上次说的那家店我去了，一般般",
        "刚到家，外面开始下雨了",
        "嗯，我也这么觉得",                    # 一条感叹开场（10% 边界内）
        "睡前翻了两页书，眼皮打架了",
    ])
    voice = flavor_report(["刚到家啦", "嗯，我也觉得是", "明天见呀"])
    checks = target_checks(overall, voice)
    assert checks and all(c["ok"] for c in checks)


def test_build_verdicts_improvement_and_warning():
    bad = {"overall": flavor_report(_bad_corpus()),
           "voice": flavor_report(["哈哈，想听我说话呀？行，就跟你聊两句，别嫌我声音哑，"
                                   "今天嗓子有点不舒服，昨晚熬夜了，不过没关系啦"]),
           "suspicion_n": 3, "reply": merge_reply_stats([])}
    good = {"overall": flavor_report(["刚到家，外面下雨了", "嗯，我也这么觉得"]),
            "voice": flavor_report(["刚到家啦", "明天见呀"]),
            "suspicion_n": 0, "reply": merge_reply_stats([])}
    improved = build_verdicts(good, bad)
    assert any("收敛" in v for v in improved)
    assert any("质疑" in v and "↓" in v for v in improved)
    regressed = build_verdicts(bad, good)
    assert any("⚠" in v for v in regressed)


# ── 周报 CLI 端到端（种子 sqlite，零生产依赖）────────────────────────────────


def _seed_inbox(db: Path, now: float) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE messages(message_id INTEGER PRIMARY KEY, conversation_id TEXT, "
        "direction TEXT, media_type TEXT, text TEXT, ts REAL)")
    d = 86400.0
    rows = [
        # 本窗（14 天内）conv c1：文字簇（已回）+ 语音簇（已回）
        ("c1", "out", "", "哈哈今天去了健身房。你吃饭了吗？", now - 3 * d),
        ("c1", "in", "", "吃了呀", now - 3 * d + 600),
        ("c1", "out", "voice", "[语音] 嘿，我刚下班呢", now - 2 * d),
        ("c1", "in", "", "辛苦啦", now - 2 * d + 3600),
        # 本窗 conv c2：文字簇（未回）+ 迟到的 AI 质疑入站
        ("c2", "out", "", "晚安啦，好梦。", now - 5 * d),
        ("c2", "in", "", "你是不是AI啊", now - 1 * d),
        # 上一窗（14~28 天前）
        ("c3", "out", "", "哈哈哈上一窗的样本。真的吗？", now - 20 * d),
    ]
    conn.executemany(
        "INSERT INTO messages(conversation_id, direction, media_type, text, ts) "
        "VALUES(?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_collect_review_end_to_end(tmp_path):
    from scripts.ai_flavor_review import collect_review, render_review, trend_row

    now = time.time()
    _seed_inbox(tmp_path / "config" / "inbox.db", now)
    rep = collect_review(tmp_path, days=14.0, now=now)
    assert rep["db_ok"] is True
    cur = rep["cur"]
    # 出站文本：1 条文字 + 1 条语音念稿 + 1 条晚安 = 3；语音切片 1 条（前缀已剥）
    assert cur["overall"]["n"] == 3
    assert cur["voice"]["n"] == 1
    assert cur["voice"]["top_openers"][0][0] == "嘿"
    # 质疑：c2 的「你是不是AI啊」
    assert cur["suspicion_n"] == 1
    assert cur["suspicion_samples"][0][0] == "direct"
    # episode：c1 文字簇回了、语音簇回了；c2 文字簇没回（质疑入站在 48h 窗外）
    assert cur["reply"]["voice"] == {"episodes": 1, "replied": 1, "reply_rate": 1.0}
    assert cur["reply"]["text"]["episodes"] == 2
    assert cur["reply"]["text"]["replied"] == 1
    # 上一窗有样本
    assert rep["prev"]["overall"]["n"] == 1
    # 渲染与趋势行不抛且关键字段在
    text = render_review(rep)
    assert "验收目标" in text and "北极星" in text
    row = trend_row(rep)
    assert row["out_n"] == 3 and row["voice_n"] == 1 and row["suspicion_n"] == 1


def test_collect_review_missing_db_soft_fail(tmp_path):
    from scripts.ai_flavor_review import collect_review, render_review

    rep = collect_review(tmp_path / "nope", days=14.0)
    assert rep["db_ok"] is False
    assert "跳过" in render_review(rep)
