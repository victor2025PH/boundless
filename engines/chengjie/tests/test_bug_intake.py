# -*- coding: utf-8 -*-
"""报障群 AI 值守门禁（bug_intake，2026-08-18）。

覆盖：配置解析 / 分类（bug 优先于 usage 的不对称语义）/ 严重度 / 危机词 /
查重归并 + 多人抬级 / 限频与收集窗三态触发 / 工单台账状态机 / 告警去抖 /
观测导出 / 四个消费点的静态接线钉（trigger 顶部三态、sender 语音压制、
skill_manager 注入+footer、ai_client 块消费、metrics 段、路由注册）。
"""
from __future__ import annotations

from pathlib import Path

import time

import pytest

ROOT = Path(__file__).resolve().parents[1]

CFG = {
    "bug_intake": {
        "enabled": True,
        "groups": [-100123],
        "support_accounts": ["777"],
        "max_replies_per_user_hour": 2,
        "max_replies_per_group_hour": 5,
        "collect_window_min": 30,
    }
}


@pytest.fixture()
def bi(tmp_path, monkeypatch):
    from src.ops import bug_intake
    from src.ops import bug_intake_backfill as bfm
    bug_intake.reset_state_for_tests()
    monkeypatch.setattr(
        bug_intake, "_db_path", lambda: tmp_path / "bug_intake.db")
    # J-5 A：已观察 mid 集与 DB 同落本测 tmp（note_group_msg_seq 会写它）
    monkeypatch.setattr(bfm, "_SEEN_PATH_OVERRIDE", tmp_path / "bug_intake_seen.json")
    yield bug_intake
    bug_intake.reset_state_for_tests()


# ── 配置解析 ───────────────────────────────────────────────────────────────────
def test_parse_cfg_defaults_off(bi):
    cfg = bi.parse_cfg({})
    assert cfg["enabled"] is False
    assert cfg["groups"] == set()
    cfg2 = bi.parse_cfg(None)
    assert cfg2["enabled"] is False


def test_parse_cfg_values_and_bad_input(bi):
    cfg = bi.parse_cfg(CFG)
    assert cfg["enabled"] and "-100123" in cfg["groups"]
    assert "777" in cfg["support_accounts"]
    assert cfg["max_replies_per_user_hour"] == 2
    # 非法值回默认，不抛
    bad = bi.parse_cfg({"bug_intake": {"enabled": True,
                                       "max_replies_per_user_hour": "x"}})
    assert bad["max_replies_per_user_hour"] == 4


def test_is_bug_group_and_voice_suppressed(bi):
    assert bi.is_bug_group(CFG, -100123)
    assert not bi.is_bug_group(CFG, -999)
    assert not bi.is_bug_group({}, -100123)
    # 语音压制：报障群 / 支持账号都压；开关关不压
    assert bi.voice_suppressed(CFG, -100123, "")
    assert bi.voice_suppressed(CFG, 555, "777")
    assert not bi.voice_suppressed(CFG, 555, "888")
    assert not bi.voice_suppressed({}, -100123, "777")


# ── 分类 ───────────────────────────────────────────────────────────────────────
def test_classify_bug_beats_usage(bi):
    # 误判代价不对称：同句含 bug 词 + 用法词 → 必须判 bug
    assert bi.classify_message("怎么回事，消息发不出去了") == "bug"
    assert bi.classify_message("语音发送失败，报错了") == "bug"
    assert bi.classify_message("crash on startup") == "bug"


def test_classify_usage_and_other(bi):
    assert bi.classify_message("怎么切换全自动模式？") == "usage"
    assert bi.classify_message("请问支持 LINE 吗") == "usage"
    assert bi.classify_message("这个功能是干嘛的？") == "usage"
    # 功能发现型（2026-08-19 实测漏网回归钉）
    assert bi.classify_message("新建人设时，人设功能区看不到语音克隆功能") == "usage"
    assert bi.classify_message("找不到导出按钮") == "usage"
    assert bi.classify_message("大家晚上好") == "other"
    assert bi.classify_message("") == "other"
    assert bi.classify_message("哈哈哈") == "other"


def test_classify_bug_settings_ineffective_and_feedback(bi):
    """2026-08-20 内测群实测漏网回归钉：设置不生效族=bug；建议/反馈族=feedback。"""
    assert bi.classify_message(
        "后台人设设置这里，默认全局人设，设定完毕之后，登陆的账号人设没有更改过来"
    ) == "bug"
    assert bi.classify_message("全局设定无效") == "bug"
    # 建议/反馈型（三连实录原文）
    assert bi.classify_message("该功能导向不明，如果非必须，建议隐藏或者删除") == "feedback"
    assert bi.classify_message("后台左侧实时日志，功能导向不明，用户端不实用") == "feedback"
    assert bi.classify_message("后台左侧开发者工具，普通用户端不需要开放，建议关闭") == "feedback"
    # 第七轮回归钉（2026-08-21 凌晨 5 条漏答实录原文，闸门压制=对用户装死）
    assert bi.classify_message(
        "选中后就显示已绑定，根本没法再点设为账号默认，这个操作逻辑有问题"
    ) == "bug"
    assert bi.classify_message(
        "后台主动关怀，只拟稿没有自动发出的原因是否是token费用问题，所以没有触发"
    ) == "bug"
    assert bi.classify_message(
        "该段提示可以删除，是针对版本调整的提示，不需要") == "feedback"
    assert bi.classify_message(
        "工具箱登记克隆音色这里的功能栏要移动到后台人设语音处去绑定，"
        "在这里进行操作设置增加了操作的难度，逻辑上也不合理") == "feedback"
    # feedback 引燃 + observe 出登记 footer 与建议值守块
    assert bi.trigger_verdict(CFG, -100123, "u7", "建议把这个页面隐藏掉") is True
    res = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u7", reporter_name="老王",
                                   text="建议把这个页面隐藏掉")
    assert res["category"] == "feedback"
    assert "建议已登记" in res["footer"]
    assert "产品建议值守" in res["prompt_block"]
    assert bi.dump_stats()["feedback"] >= 1
    # 语言锚（2026-08-20 英文截图带偏值守语言实录）：所有值守块统一携带
    assert "工作语言为中文" in res["prompt_block"]


def test_severity_ladder(bi):
    assert bi.classify_severity("软件崩溃打不开了") == "P0"
    assert bi.classify_severity("消息发不出去") == "P1"
    assert bi.classify_severity("按钮颜色显示不对") == "P2"


def test_crisis_words(bi):
    assert bi.is_crisis("你们这是诈骗，我要退款")
    assert not bi.is_crisis("翻译有点慢")


def test_titles_similar(bi):
    assert bi.titles_similar("消息发不出去了！", "消息发不出去")
    assert bi.titles_similar("语音 发送失败", "语音发送失败？？")
    assert not bi.titles_similar("语音发送失败", "头像上传之后不显示")
    assert not bi.titles_similar("", "x")


# ── 工单台账 ───────────────────────────────────────────────────────────────────
def test_ticket_new_dup_and_severity_uplift(bi):
    r1 = bi.record_bug_ticket(chat_id=-100123, account_id="777",
                              reporter_id="u1", reporter_name="张三",
                              text="消息发不出去了")
    assert r1["is_new"] and r1["ticket_id"] > 0 and r1["severity"] == "P1"
    # 相似标题 → 归并 +1，不新开单
    r2 = bi.record_bug_ticket(chat_id=-100123, account_id="777",
                              reporter_id="u2", reporter_name="李四",
                              text="消息发不出去")
    assert not r2["is_new"] and r2["ticket_id"] == r1["ticket_id"]
    assert r2["report_count"] == 2
    # 不同群不归并
    r3 = bi.record_bug_ticket(chat_id=-999, account_id="777",
                              reporter_id="u3", reporter_name="",
                              text="消息发不出去")
    assert r3["is_new"]


def test_ticket_p2_uplift_at_three_reports(bi):
    for i, expect_new in ((1, True), (2, False), (3, False)):
        r = bi.record_bug_ticket(chat_id=-100123, account_id="",
                                 reporter_id=f"u{i}", reporter_name="",
                                 text="表情面板图标显示不对")
    assert r["report_count"] == 3
    assert r["severity"] == "P1"  # P2 三人报告抬到 P1


def test_ticket_status_machine_and_list(bi):
    r = bi.record_bug_ticket(chat_id=-100123, account_id="",
                             reporter_id="u1", reporter_name="",
                             text="崩溃闪退")
    tid = r["ticket_id"]
    assert bi.set_ticket_status(tid, "confirmed")
    assert not bi.set_ticket_status(tid, "nonsense")
    assert not bi.set_ticket_status(999999, "fixed")
    rows = bi.list_tickets(status="confirmed")
    assert any(int(x["id"]) == tid for x in rows)
    bi.append_ticket_note(tid, "补充：版本 1.0.39")
    row = [x for x in bi.list_tickets() if int(x["id"]) == tid][0]
    assert "1.0.39" in row["body"]


# ── 触发三态 ───────────────────────────────────────────────────────────────────
def test_verdict_none_outside_bug_group(bi):
    assert bi.trigger_verdict(CFG, -999, "u1", "报错了") is None
    assert bi.trigger_verdict({}, -100123, "u1", "报错了") is None


def test_verdict_engage_on_keywords_and_defer_on_chatter(bi):
    assert bi.trigger_verdict(CFG, -100123, "u1", "登录不上了") is True
    assert bi.trigger_verdict(CFG, -100123, "u1", "怎么换人设？") is True
    assert bi.trigger_verdict(CFG, -100123, "u1", "智聊支持 在吗") is True
    # 闲聊：静默压制（2026-08-20 串戏事故收口——落回原生 follow_window 链
    # 会让支持号用陪伴人设接话）；@提及/回复本账号（is_direct）仍接话
    assert bi.trigger_verdict(CFG, -100123, "u1", "大家晚上好") is False
    assert bi.dump_stats()["smalltalk_suppressed"] >= 1
    assert bi.trigger_verdict(CFG, -100123, "u1", "大家晚上好",
                              is_direct=True) is True
    # 报障群外不受影响：闲聊仍放行原链
    assert bi.trigger_verdict(CFG, -999, "u1", "大家晚上好") is None


def test_self_sender_never_registers_or_engages(bi):
    """2026-09-02 收口：本方账号消息绝不立单/引燃（#123/#125/#135/#136 实锤——
    值守号 8506426282 的回访播报满是 bug 词，被 6834964252 的观察链误立单）。"""
    # support_accounts 里的支持号发回访播报 → trigger 硬压制
    assert bi.trigger_verdict(CFG, -100123, "777", "消息发不出去了，报错") is False
    # observe 立单入口：不立单、active=False（调用方零分支透传）
    res = bi.observe_group_message(
        CFG, chat_id=-100123, account_id="999", reporter_id="777",
        reporter_name="BOUNDLESS",
        text="你反馈的「消息发不出去」（#12）已修复上线，方便的话帮忙验证一下")
    assert res["active"] is False and res["ticket_id"] == 0
    assert bi.list_tickets() == []
    # 本 worker 自己的 account_id（补拉链回喂形态）同样拦
    res2 = bi.observe_group_message(
        CFG, chat_id=-100123, account_id="888", reporter_id="888",
        reporter_name="自己", text="登录不上，全部崩溃了")
    assert res2["active"] is False
    assert bi.list_tickets() == []
    assert bi.dump_stats()["self_msg_suppressed"] >= 3
    # 普通用户不受守卫影响：照常立单
    res3 = bi.observe_group_message(
        CFG, chat_id=-100123, account_id="999", reporter_id="u1",
        reporter_name="张三", text="消息发不出去了")
    assert res3["active"] and res3["ticket_id"] > 0


class _BusStub:
    def __init__(self):
        self.published = []

    def publish(self, etype, payload):
        self.published.append((etype, payload))


@pytest.fixture()
def bus(monkeypatch):
    from src.integrations.shared import event_bus as eb
    stub = _BusStub()
    monkeypatch.setattr(eb, "get_event_bus", lambda: stub)
    return stub


def test_verdict_crisis_holds_and_alerts_once(bi, bus):
    assert bi.trigger_verdict(CFG, -100123, "u9", "你们是骗子我要退款") is False
    assert bi.trigger_verdict(CFG, -100123, "u9", "退款！骗子！") is False
    assert len(bus.published) == 1  # 同用户 30min 去抖，只告警一次
    etype, payload = bus.published[0]
    assert etype == "bug_intake_alert" and payload["kind"] == "crisis"
    assert payload.get("rate_key", "").startswith("bug_intake:")


def test_verdict_rate_cap_suppresses_even_mentions(bi):
    # observe 计数两次（配置 cap=2）→ 第三条即便是关键词也压制
    for i in range(2):
        bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                 reporter_id="u1", reporter_name="张三",
                                 text=f"发不出消息 变体{i}梯度不同标题写法")
    assert bi.trigger_verdict(CFG, -100123, "u1", "还是报错") is False


def test_collect_window_keeps_conversation_alive(bi):
    res = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u1", reporter_name="张三",
                                   text="语音发送失败")
    assert res["category"] == "bug" and res["is_new"]
    # 收集窗内的无关键词跟进（纯版本号）也引燃
    assert bi.trigger_verdict(CFG, -100123, "u1", "1.0.39") is True
    follow = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                      reporter_id="u1", reporter_name="张三",
                                      text="版本是 1.0.39")
    assert follow["category"] == "collect"
    assert follow["ticket_id"] == res["ticket_id"]
    row = [x for x in bi.list_tickets()
           if int(x["id"]) == res["ticket_id"]][0]
    assert "1.0.39" in row["body"]


# ── observe 语义 ───────────────────────────────────────────────────────────────
def test_observe_inactive_outside_group(bi):
    out = bi.observe_group_message({}, chat_id=-100123, account_id="",
                                   reporter_id="u", reporter_name="", text="x")
    assert out["active"] is False and out["prompt_block"] == ""


def test_observe_bug_has_footer_and_block(bi, bus):
    out = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u1", reporter_name="张三",
                                   text="软件崩溃打不开")
    assert out["active"] and out["category"] == "bug"
    assert f"#{out['ticket_id']}" in out["footer"]
    assert "工单" in out["prompt_block"]
    assert bus.published, "P0 新工单应即时告警"
    assert bus.published[0][0] == "bug_intake_alert"
    assert bus.published[0][1]["kind"] == "ticket"
    # 第二人同报 → dup footer 带「并入」
    out2 = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                    reporter_id="u2", reporter_name="李四",
                                    text="软件崩溃 打不开")
    assert not out2["is_new"] and "并入" in out2["footer"]


# ── P2：修复回访 + 遥测快照 ───────────────────────────────────────────────────
def test_fix_notify_text_mention_and_escape(bi):
    row = {"id": 12, "reporter_id": "12345", "reporter_name": "张<三>",
           "title": "语音<b>发送失败"}
    txt = bi.build_fix_notify_text(row)
    assert 'tg://user?id=12345' in txt
    assert "#12" in txt and "已修复" in txt
    assert "<b>" not in txt and "张&lt;三&gt;" in txt  # HTML 注入被转义
    # reporter_id 非数字 → 退化纯文本，不带深链
    row2 = {"id": 3, "reporter_id": "tg:abc", "reporter_name": "李四",
            "title": "x"}
    assert "tg://user" not in bi.build_fix_notify_text(row2)


def test_mark_notified_and_columns(bi):
    r = bi.record_bug_ticket(chat_id=-100123, account_id="777",
                             reporter_id="u1", reporter_name="",
                             text="卡死了")
    tid = r["ticket_id"]
    bi.mark_notified(tid, False, "worker_offline")
    row = bi.get_ticket(tid)
    assert row["notify_ts"] == 0 and row["notify_note"] == "worker_offline"
    bi.mark_notified(tid, True, "")
    row = bi.get_ticket(tid)
    assert row["notify_ts"] > 0


def test_telemetry_snapshot_best_effort(bi):
    # 无论遥测源是否可用都不得抛异常，返回 str（可为空）
    out = bi._telemetry_snapshot()
    assert isinstance(out, str)


def test_wiring_notify_routes():
    src = _read("src/web/routes/bug_intake_routes.py")
    assert "/api/admin/bug-intake/{ticket_id}/notify" in src
    assert "_send_group_notify" in src
    # fixed 状态自动尝试回访（失败不阻塞流转）
    assert 'if status == "fixed":' in src


# ── P3：verified 自动闭环 ─────────────────────────────────────────────────────
def test_detect_verify_intent_semantics(bi):
    assert bi.detect_verify_intent("好了，可以用了") == "yes"
    assert bi.detect_verify_intent("修复了，谢谢") == "yes"
    assert bi.detect_verify_intent("还是不行啊") == "no"
    assert bi.detect_verify_intent("更新后还是报错") == "no"
    # 疑问句弃权：「修复了吗」是追问不是确认
    assert bi.detect_verify_intent("修复了吗？") == ""
    assert bi.detect_verify_intent("好了吗") == ""
    assert bi.detect_verify_intent("今天天气好") == ""
    assert bi.detect_verify_intent("") == ""


def _mk_fixed_notified(bi, reporter="u1", text="消息发不出去"):
    r = bi.record_bug_ticket(chat_id=-100123, account_id="777",
                             reporter_id=reporter, reporter_name="张三",
                             text=text)
    tid = r["ticket_id"]
    bi.set_ticket_status(tid, "fixed")
    bi.mark_notified(tid, True, "")
    return tid


def test_verify_yes_closes_ticket(bi, bus):
    tid = _mk_fixed_notified(bi)
    # 无报障关键词的确认话也要引燃（verdict 走 verify 路径）
    assert bi.trigger_verdict(CFG, -100123, "u1", "好了，能用了") is True
    # J-5 B（D5）：归属靶文中 #N
    out = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u1", reporter_name="张三",
                                   text=f"#{tid} 好了，能用了")
    assert out["category"] == "verify"
    assert "✅" in out["footer"] and f"#{tid}" in out["footer"]
    assert bi.get_ticket(tid)["status"] == "verified"
    ev = bi.list_events(["verify_yes"])
    assert len(ev) == 1 and "via=hash" in ev[0]["detail"]


def test_verify_no_reopens_and_rearms_collect(bi, bus):
    tid = _mk_fixed_notified(bi, reporter="u2")
    out = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u2", reporter_name="李四",
                                   text=f"更新了 #{tid} 还是不行")
    assert out["category"] == "verify" and "🔁" in out["footer"]
    row = bi.get_ticket(tid)
    assert row["status"] == "confirmed"
    assert "验证未过" in row["body"]
    # 收集窗已重新拉起：后续描述补录进同一单
    follow = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                      reporter_id="u2", reporter_name="李四",
                                      text="现在是点了发送没反应")
    assert follow["category"] == "collect" and follow["ticket_id"] == tid


def test_verify_needs_notified_ticket(bi):
    # fixed 但未回访（notify_ts=0）→ 不判定确认（用户没收到过回访就说好了=巧合语）
    r = bi.record_bug_ticket(chat_id=-100123, account_id="777",
                             reporter_id="u3", reporter_name="",
                             text="卡死了")
    bi.set_ticket_status(r["ticket_id"], "fixed")
    assert bi.pending_verify_ticket(-100123, "u3") is None
    assert bi.candidate_verify_tickets(-100123, "u3") == []


# ── J-5 B（决策 D5）：verify 归属只认 #N / reply_to，裸短句不翻单 ─────────────
def _observe(bi, reporter, text, **kw):
    return bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                    reporter_id=reporter, reporter_name="钧",
                                    text=text, **kw)


def test_extract_ticket_refs_and_resolve_pure():
    from src.ops import bug_intake as bi
    assert bi.extract_ticket_refs("#142 好了") == [142]
    assert bi.extract_ticket_refs("# 140 和 #142 都可以了，#142") == [140, 142]
    assert bi.extract_ticket_refs("已经可以了") == []
    cands = [{"id": 142, "notify_msg_id": 9001},
             {"id": 140, "notify_msg_id": 9000},
             {"id": 138, "notify_msg_id": 0}]
    assert bi.resolve_verify_target("#140 好了", cands)[0]["id"] == 140
    assert bi.resolve_verify_target("#140 好了", cands)[1] == "hash"
    # #N 优先于 reply_to（回着 A 的回访说「#B 好了」以明说为准）
    assert bi.resolve_verify_target("#138 好了", cands, 9001)[0]["id"] == 138
    assert bi.resolve_verify_target("好了", cands, 9001) == (cands[0], "reply")
    assert bi.resolve_verify_target("好了", cands, 9000)[0]["id"] == 140
    assert bi.resolve_verify_target("好了", cands, 0) == (None, "no_anchor")
    assert bi.resolve_verify_target("好了", cands, 777) == (None, "no_anchor")
    assert bi.resolve_verify_target("#999 好了", cands) == (None, "hash_unknown")
    assert bi.resolve_verify_target("好了", []) == (None, "no_candidates")


def test_verify_hash_targets_named_ticket_not_latest(bi, bus):
    """0905 实锤：钧说的是 WhatsApp 登录那张，却翻了 id 最大的总闸单。现在
    「#<老单> 好了」必须翻老单、新单原地不动。"""
    old = _mk_fixed_notified(bi, text="WhatsApp 登录不了")
    new = _mk_fixed_notified(bi, text="总闸打不开")
    assert new > old
    out = _observe(bi, "u1", f"#{old} 已经可以了")
    assert out["category"] == "verify" and out["ticket_id"] == old
    assert bi.get_ticket(old)["status"] == "verified"
    assert bi.get_ticket(new)["status"] == "fixed"


def test_verify_bare_short_phrase_is_ambiguous_not_flipped(bi, bus):
    """「已经可以了」无 #N 无 reply_to → 一张单都不翻，记 verify_ambiguous 事件
    （值守可见），消息本身照常走普通链（不是被吞掉）。"""
    a = _mk_fixed_notified(bi, text="WhatsApp 登录不了")
    b = _mk_fixed_notified(bi, text="总闸打不开")
    n_yes = bi.dump_stats()["verify_yes"]
    out = _observe(bi, "u1", "已经可以了，看是否可以修复，稳定一点的")
    assert out["active"] is True and out["category"] != "verify"
    assert bi.get_ticket(a)["status"] == "fixed"
    assert bi.get_ticket(b)["status"] == "fixed"
    assert bi.dump_stats()["verify_yes"] == n_yes
    assert bi.dump_stats()["verify_ambiguous"] == 1
    ev = bi.list_events(["verify_ambiguous"])
    assert len(ev) == 1
    assert ev[0]["detail"].startswith("yes:no_anchor")
    assert f"#{a}" in ev[0]["detail"] and f"#{b}" in ev[0]["detail"]
    assert bi.list_events(["verify_yes"]) == []


def test_verify_reply_to_notify_message_attributes_that_ticket(bi, bus):
    """回着 bot 的回访消息说「好了」→ 归属该回访对应的单（哪怕它不是最新）。"""
    old = _mk_fixed_notified(bi, text="WhatsApp 登录不了")
    new = _mk_fixed_notified(bi, text="总闸打不开")
    bi.mark_notified(old, True, "", msg_id=5100)
    bi.mark_notified(new, True, "", msg_id=5200)
    out = _observe(bi, "u1", "好了，能用了", reply_to_msg_id=5100)
    assert out["category"] == "verify" and out["ticket_id"] == old
    assert bi.get_ticket(old)["status"] == "verified"
    assert bi.get_ticket(new)["status"] == "fixed"
    ev = bi.list_events(["verify_yes"])
    assert len(ev) == 1 and "via=reply" in ev[0]["detail"]
    # 回的不是回访消息（随便回了群里别的话）→ 不归属
    out2 = _observe(bi, "u1", "还是不行", reply_to_msg_id=4242,
                    now=time.time() + 120)
    assert out2["category"] != "verify"
    assert bi.get_ticket(new)["status"] == "fixed"
    assert bi.dump_stats()["verify_ambiguous"] == 1


def test_verify_hash_unknown_ticket_does_not_flip_others(bi, bus):
    """带了号但不是该报障人的待验单（别人的 / 打错）→ 不翻任何单，记 ambiguous。"""
    mine = _mk_fixed_notified(bi, text="消息发不出去")
    out = _observe(bi, "u1", "#999 好了")
    assert out["category"] != "verify"
    assert bi.get_ticket(mine)["status"] == "fixed"
    ev = bi.list_events(["verify_ambiguous"])
    assert len(ev) == 1 and ev[0]["detail"].startswith("yes:hash_unknown")


def test_verify_ambiguous_silent_when_no_candidates(bi, bus):
    """没有任何待验单时「好了」就是闲聊：不记 ambiguous（否则群里每句好了都
    进值守面板＝噪音）。"""
    _observe(bi, "u9", "好了")
    assert bi.dump_stats()["verify_ambiguous"] == 0
    assert bi.list_events(["verify_ambiguous"]) == []


def test_backfill_row_reply_to_passthrough(bi, bus, tmp_path):
    """回放链把行里的 reply_to 透传进 observe → 归属同实时链口径。"""
    from src.ops import bug_intake_backfill as bf
    old = _mk_fixed_notified(bi, text="WhatsApp 登录不了")
    new = _mk_fixed_notified(bi, text="总闸打不开")
    bi.mark_notified(old, True, "", msg_id=6100)
    bi.mark_notified(new, True, "", msg_id=6200)
    cfg = dict(CFG)
    cfg["bug_intake"] = dict(CFG["bug_intake"])
    cfg["bug_intake"]["backfill"] = {"enabled": True, "cap": 30}
    sp = tmp_path / "bf.state.json"
    bf.save_state({"-100123": {"last_msg_id": 7000, "ts": 1.0}}, sp)

    async def fetch(chat_id, cap):
        return [{"id": 7001, "text": "好了，能用了", "reporter_id": "u1",
                 "reporter_name": "钧", "outgoing": False, "has_media": False,
                 "reply_to_id": "6100"}]

    s = _run(bf.run_backfill_once(cfg, fetch, account_id="777", state_path=sp))
    assert s["replayed"] == 1
    assert bi.get_ticket(old)["status"] == "verified"
    assert bi.get_ticket(new)["status"] == "fixed"


# ── P3：截图收集 + 回访积压 ───────────────────────────────────────────────────
def test_note_screenshot_in_window(bi, bus):
    res = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u1", reporter_name="张三",
                                   text="语音发送失败")
    tid = res["ticket_id"]
    assert bi.note_screenshot(CFG, -100123, "u1") is True
    assert "[截图已收到]" in bi.get_ticket(tid)["body"]
    # 无收集窗 / 非报障群 → False
    assert bi.note_screenshot(CFG, -100123, "u_nobody") is False
    assert bi.note_screenshot({}, -100123, "u1") is False


def test_pure_photo_does_not_engage_but_records(bi, bus):
    res = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u1", reporter_name="张三",
                                   text="报错了")
    # 纯图（无文字）：verdict 记截图并**静默**（2026-08-20 起不再放回原生链
    # ——follow_window 会把用户截图当闲图点评）……收集窗内文字仍引燃
    v = bi.trigger_verdict(CFG, -100123, "u1", "", has_photo=True)
    assert v is False
    assert bi.dump_stats()["photo_suppressed"] >= 1
    assert "[截图已收到]" in bi.get_ticket(res["ticket_id"])["body"]
    assert bi.trigger_verdict(CFG, -100123, "u1", "1.0.39") is True


def test_list_pending_notify(bi):
    r = bi.record_bug_ticket(chat_id=-100123, account_id="777",
                             reporter_id="u1", reporter_name="",
                             text="白屏了")
    bi.set_ticket_status(r["ticket_id"], "fixed")  # 未 mark_notified
    rows = bi.list_pending_notify()
    assert any(int(x["id"]) == r["ticket_id"] for x in rows)
    assert bi.dump_stats()["pending_notify"] >= 1
    bi.mark_notified(r["ticket_id"], True, "")
    assert not any(int(x["id"]) == r["ticket_id"]
                   for x in bi.list_pending_notify())


def test_wiring_kb_gate_bug_group_exempt():
    # conversion 域「防推销」KB 闸必须豁免报障群（2026-08-18 实测误伤：
    # 「怎么登录」的正确 KB 命中被丢弃成泛答）
    src = _read("src/skills/skill_manager.py")
    assert "_bug_group_kb_exempt" in src
    seg = src.split("dropped KB inject", 1)[0]
    assert "is_bug_group" in seg


def test_wiring_photo_and_pending_routes():
    tsrc = _read("src/client/trigger.py")
    assert "has_photo=bool(getattr(message, \"photo\", None))" in tsrc
    rsrc = _read("src/web/routes/bug_intake_routes.py")
    assert "/api/admin/bug-intake/notify-pending" in rsrc


# ── P2：周读 CLI 纯函数 ────────────────────────────────────────────────────────
def test_report_cluster_and_summary(bi):
    sys_path_added = str(ROOT) not in __import__("sys").path
    if sys_path_added:
        __import__("sys").path.insert(0, str(ROOT))
    from tools.bug_intake_report import cluster_usage_questions, summarize
    events = [
        {"kind": "usage", "detail": "怎么切换全自动模式？"},
        {"kind": "usage", "detail": "怎么切换全自动模式"},
        {"kind": "usage", "detail": "语音在哪里发"},
        {"kind": "bug_new", "detail": "崩溃"},
        {"kind": "crisis_hold", "detail": "退款"},
    ]
    clusters = cluster_usage_questions(events)
    assert clusters[0]["count"] == 2  # 相似用法问题聚成一簇
    assert all(c["count"] >= 1 for c in clusters)
    tickets = [
        {"severity": "P1", "status": "fixed", "notify_ts": 123.0,
         "report_count": 3, "id": 1, "title": "发不出"},
        {"severity": "P2", "status": "new", "notify_ts": 0,
         "report_count": 1, "id": 2, "title": "颜色不对"},
    ]
    s = summarize({"tickets": tickets, "events": events}, 7)
    assert s["tickets_total"] == 2
    assert s["usage_questions"] == 3 and s["bug_reports"] == 1
    assert s["fix_notify"] == {"fixed": 1, "notified": 1}
    assert s["top_duplicated"][0]["id"] == 1
    assert s["kb_suggestions"]


def test_observe_usage_no_ticket_no_footer(bi):
    out = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u1", reporter_name="",
                                   text="怎么切换全自动？")
    assert out["category"] == "usage"
    assert out["ticket_id"] == 0 and out["footer"] == ""
    assert "答疑" in out["prompt_block"]


def test_dump_stats_shape(bi):
    bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                             reporter_id="u1", reporter_name="",
                             text="卡死了")
    d = bi.dump_stats()
    assert d["active"] is True
    assert d["tickets_24h"] >= 1
    assert isinstance(d["open_by_severity"], dict)
    assert d["observed"] >= 1


# ── 静态接线钉（四消费点 + metrics + 路由注册）────────────────────────────────
def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_wiring_trigger_tri_state_before_reply_chain():
    src = _read("src/client/trigger.py")
    body = src.split("async def _should_reply_to_group_message", 1)[1]
    assert "from src.ops.bug_intake import trigger_verdict" in body
    # 三态判定必须先于原生链返回（压制要能盖住 @提及/回复链/follow_window）：
    # verdict 的 return 在任何 _trigger_path 赋值之前
    assert body.index("trigger_verdict") < body.index(
        '_trigger_path = "reply_chain"')
    # 点名信号必须传给裁决者（@提及/回复本账号在报障群内仍要接话）
    assert "is_direct=_bi_direct" in body


def test_wiring_sender_voice_suppressed():
    src = _read("src/client/sender.py")
    assert "from src.ops.bug_intake import voice_suppressed" in src


def test_wiring_skill_manager_observe_and_footer():
    src = _read("src/skills/skill_manager.py")
    assert "from src.ops.bug_intake import observe_group_message" in src
    assert '_bug_intake_block' in src
    # footer 追加必须在媒体承诺守卫之后（终稿层）
    idx_guard = src.index("_apply_media_promise_guard(\n                    reply")
    idx_footer = src.index('_bug_intake_res.get("footer")')
    assert idx_footer > idx_guard


def test_wiring_ai_client_consumes_block():
    src = _read("src/ai/ai_client.py")
    assert '_bug_intake_block' in src


def test_wiring_metrics_segment():
    src = _read("src/web/routes/drafts_routes.py")
    assert 'metrics["bug_intake"]' in src


def test_wiring_routes_registered():
    src = _read("src/web/admin.py")
    assert "register_bug_intake_routes" in src


# ── B33（实施49 2026-08-21）：限频拦真反馈 → 先登记再压制 + 截图归档登记 ────────
# 事故：闲聊风暴烧光用户小时预算 → 6 条真反馈被拦（06:33-06:38 实录），文字靠
# 人工 sync 才还原、随图媒体永久丢失。契约：回复照拦（防刷屏语义不变，上方
# test_verdict_rate_cap_suppresses_even_mentions 继续成立），但报障全文进
# bug_events 台账、收集窗截图照记、trigger 层另把被压制的图归档 protocol_media。


def _events_of(bi, kind):
    con = bi._db()
    return [
        {"chat_id": r[0], "reporter": r[1], "detail": r[2]}
        for r in con.execute(
            "SELECT chat_id, reporter_id, detail FROM bug_events WHERE kind=?",
            (kind,)).fetchall()
    ]


def test_b33_rate_capped_report_recorded_but_still_suppressed(bi):
    for i in range(2):
        bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                 reporter_id="u1", reporter_name="张三",
                                 text=f"发不出消息 变体{i}")
    # 超额后的真报障：仍压制（防刷屏契约不变）……但全文进台账可追回
    assert bi.trigger_verdict(CFG, -100123, "u1", "语音发送失败，报错了") is False
    evs = _events_of(bi, "rate_capped_report")
    assert len(evs) == 1 and "语音发送失败" in evs[0]["detail"]
    assert bi._STATS["rate_capped_report"] == 1


def test_b33_rate_capped_smalltalk_not_recorded(bi):
    # 直接烧光预算（不经 observe——observe 建工单会开收集窗，窗内跟进按报障
    # 材料对待是既有语义，不属本例要测的「纯闲聊不登记」）
    import time as _t
    now = _t.time()
    bi._rate_mark("u:-100123:u2", now)
    bi._rate_mark("u:-100123:u2", now)
    assert bi.trigger_verdict(CFG, -100123, "u2", "哈哈今天天气不错") is False
    assert _events_of(bi, "rate_capped_report") == []


def test_b33_capped_photo_ledger(bi):
    bi.record_capped_photo(-100123, "u9", "/static/protocol_media/x.jpg")
    evs = _events_of(bi, "capped_photo_archived")
    assert len(evs) == 1 and evs[0]["detail"].endswith("x.jpg")
    assert bi._STATS["capped_photo_archived"] == 1


def test_b33_capped_report_with_photo_notes_screenshot(bi):
    # 建工单（开收集窗）→ 烧光预算 → 带图跟进被拦：截图仍记进工单证据链
    res = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u1", reporter_name="张三",
                                   text="语音发送失败")
    bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                             reporter_id="u1", reporter_name="张三",
                             text="补充：弹了报错窗")
    assert bi.trigger_verdict(CFG, -100123, "u1", "看这张报错截图",
                              has_photo=True) is False
    evs = _events_of(bi, "screenshot")
    assert any(f"#{res['ticket_id']}" in e["detail"] for e in evs)


def test_b33_wiring_trigger_archives_capped_photo():
    src = _read("src/client/trigger.py")
    assert "_bug_intake_archive_capped_photo" in src
    assert "record_capped_photo" in src


# ── AI 全静默（2026-08-21 11:05 老板纪律 B36 收紧版：报障群登记/回复/整理全部
#    由值守人工来，本地模型与云端一条不发；observe 台账照记）──────────────────
def test_ai_silent_marks_all_categories_but_still_records(bi, bus):
    out = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u1", reporter_name="张三",
                                   text="软件崩溃打不开")
    assert out["ai_silent"] is True
    assert out["ticket_id"] > 0, "静默≠不登记：工单照记（值守台账工具）"
    assert bus.published, "P0 告警照发"
    fol = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u1", reporter_name="张三",
                                   text="版本是 1.0.45")
    assert fol["category"] == "collect" and fol["ai_silent"] is True
    for uid, txt in (("u3", "建议把这个页面隐藏掉"), ("u4", "怎么切换全自动？"),
                     ("u9", "今天天气不错啊")):
        r = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                     reporter_id=uid, reporter_name="某人",
                                     text=txt)
        assert r["ai_silent"] is True, txt


def test_ai_silent_flag_off_restores_llm_path(bi):
    import copy
    cfg2 = copy.deepcopy(CFG)
    cfg2["bug_intake"]["ai_silent"] = False
    out = bi.observe_group_message(cfg2, chat_id=-100123, account_id="777",
                                   reporter_id="u8", reporter_name="路人乙",
                                   text="软件卡死了")
    assert out["ai_silent"] is False and out["prompt_block"]


def test_ai_silent_inactive_passthrough(bi):
    out = bi.observe_group_message({}, chat_id=-100123, account_id="",
                                   reporter_id="u", reporter_name="", text="x")
    assert out["active"] is False and out["ai_silent"] is False


def test_wiring_skill_manager_silences_before_generation():
    src = _read("src/skills/skill_manager.py")
    idx_silent = src.index('_bug_intake_res.get("ai_silent")')
    idx_footer = src.index('_bug_intake_res.get("footer")')
    assert idx_silent < idx_footer, "静默短路必须在生成链之前（不是终稿层丢草稿）"
    seg = src[idx_silent:idx_silent + 400]
    assert "return None" in seg, "静默=返回 None（与超时路径同契约）"


# ── C1-②（#123 族，2026-09-02）：群消息序号连续性哨兵 ────────────────────────
# 0902 21:1x 实锤：mid=1084 漏收、1085 正常到达，顶部比对的 gap-probe 报
# gap=false。哨兵按「本条 mid > 上一条 +1」判候选洞，宽限期后告警+交补拉。

def test_seq_sentinel_first_seen_only_sets_watermark(bi):
    assert bi.note_group_msg_seq(CFG, -100123, 1083, account_id="a", now=100.0) == []
    snap = bi.seq_sentinel_snapshot()
    assert snap["a:-100123"] == {"last_mid": 1083, "pending": 0}


def test_seq_sentinel_detects_skipped_mid_and_harvests_after_grace(bi, bus):
    bi.note_group_msg_seq(CFG, -100123, 1083, account_id="a", now=100.0)
    # 1084 没到，1085 到了 → 1084 是候选洞
    assert bi.note_group_msg_seq(CFG, -100123, 1085, account_id="a", now=101.0) == [1084]
    # 宽限期内不收割（乱序容忍）
    assert bi.due_seq_gaps(CFG, now=101.0 + 5) == []
    assert bus.published == []
    # 宽限期过 → 落台账 + 告警 + 返回补拉计划
    due = bi.due_seq_gaps(CFG, now=101.0 + bi.seq_grace_sec() + 0.5)
    assert due == [{"account_id": "a", "chat_id": "-100123",
                    "missing_ids": [1084], "last_mid": 1085}]
    assert len(bus.published) == 1
    etype, payload = bus.published[0]
    assert etype == "bug_intake_alert" and payload["kind"] == "seq_gap"
    assert payload["missing_ids"] == [1084]
    assert payload["rate_key"] == "bug_intake:seq_gap:-100123:1084"
    evs = bi.list_events(["seq_gap"])
    assert len(evs) == 1 and "missing=1084" in evs[0]["detail"]
    # 收割过的洞不会二次收割
    assert bi.due_seq_gaps(CFG, now=999.0) == []
    assert bi.dump_stats()["seq_gap"] == 1


def test_seq_sentinel_late_arrival_heals_hole_without_alert(bi, bus):
    bi.note_group_msg_seq(CFG, -100123, 10, account_id="a", now=100.0)
    assert bi.note_group_msg_seq(CFG, -100123, 12, account_id="a", now=100.5) == [11]
    # 11 晚到（乱序）→ 洞自愈
    assert bi.note_group_msg_seq(CFG, -100123, 11, account_id="a", now=101.0) == []
    assert bi.due_seq_gaps(CFG, now=200.0) == []
    assert bus.published == []
    # 重投/编辑同号：无信息量
    assert bi.note_group_msg_seq(CFG, -100123, 12, account_id="a", now=102.0) == []


def test_seq_sentinel_big_jump_not_expanded_and_non_bug_group_noop(bi, bus):
    bi.note_group_msg_seq(CFG, -100123, 10, account_id="a", now=100.0)
    # 大跳号（停机窗）：不逐 id 展开（归顶部缺口链/backfill），水位照推
    assert bi.note_group_msg_seq(CFG, -100123, 10 + 500, account_id="a", now=101.0) == []
    assert bi.seq_sentinel_snapshot()["a:-100123"]["pending"] == 0
    assert bi.seq_sentinel_snapshot()["a:-100123"]["last_mid"] == 510
    # 非报障群 / 关闭 / 坏 id：全 no-op
    assert bi.note_group_msg_seq(CFG, -999, 5, account_id="a") == []
    assert bi.note_group_msg_seq({}, -100123, 5, account_id="a") == []
    assert bi.note_group_msg_seq(CFG, -100123, "abc", account_id="a") == []
    assert "a:-999" not in bi.seq_sentinel_snapshot()


def test_seq_sentinel_keys_per_account(bi):
    """同群多账号各自一套水位（worker 各有各的到达面）；收割按账号过滤——
    别把别人的洞摘走却不补。"""
    bi.note_group_msg_seq(CFG, -100123, 10, account_id="a", now=1.0)
    bi.note_group_msg_seq(CFG, -100123, 10, account_id="b", now=1.0)
    assert bi.note_group_msg_seq(CFG, -100123, 12, account_id="a", now=2.0) == [11]
    assert bi.note_group_msg_seq(CFG, -100123, 13, account_id="b", now=2.0) == [11, 12]
    late = 2.0 + bi.seq_grace_sec() + 1
    # b 的 sweep 只收 b 的洞；a 的洞原地不动
    due_b = bi.due_seq_gaps(CFG, now=late, account_id="b")
    assert [(d["account_id"], d["missing_ids"]) for d in due_b] == [("b", [11, 12])]
    assert bi.seq_sentinel_snapshot()["a:-100123"]["pending"] == 1
    due_a = bi.due_seq_gaps(CFG, now=late, account_id="a")
    assert [(d["account_id"], d["missing_ids"]) for d in due_a] == [("a", [11])]
    assert bi.due_seq_gaps(CFG, now=late) == []


def test_seq_gap_backfilled_ledger(bi):
    bi.seq_gap_backfilled(-100123, [1084, 1086], filled=1, empty=1)
    evs = bi.list_events(["seq_gap_backfill"])
    assert len(evs) == 1
    assert "filled=1" in evs[0]["detail"] and "empty=1" in evs[0]["detail"]
    st = bi.dump_stats()
    assert st["seq_gap_filled"] == 1 and st["seq_gap_empty"] == 1


def test_wiring_telegram_client_seq_sentinel_and_replay():
    """接线钉：handler 顶部登记 + 补拉重放走同一个 handler（不另起半链）。"""
    src = _read("src/client/telegram_client.py")
    i_note = src.index("note_group_msg_seq(")
    i_claim = src.index("self._msg_dedup.claim(chat_id, mid)")
    assert i_note < i_claim, "哨兵必须在去重 claim 之前（任何真到达的 id 都算见过）"
    assert "self._group_message_handler = handle_group_message" in src
    assert "fetch_tg_messages_by_ids" in src
    assert "seq_gap_backfilled(" in src
    src_wh = _read("src/inbox/webhook_notifier.py")
    assert '== "seq_gap"' in src_wh, "告警 formatter 必须认识 seq_gap（否则渲染成假「新工单」）"


# ── J-5 A（2026-09-05）：backfill 二次观察收口 ─────────────────────────────────

def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_seq_sentinel_marks_seen_for_backfill(bi):
    """实时链 note_group_msg_seq 一到即登记「已观察」（早于去重/限频/observe）。"""
    from src.ops.bug_intake_backfill import is_seen
    bi.note_group_msg_seq(CFG, -100123, 500, account_id="777", now=1.0)
    assert is_seen(-100123, 500)
    # 非报障群 / 无效 id 不登记
    bi.note_group_msg_seq(CFG, -100999, 7, account_id="777", now=1.0)
    bi.note_group_msg_seq(CFG, -100123, 0, account_id="777", now=1.0)
    assert not is_seen(-100999, 7) and not is_seen(-100123, 0)


def test_observe_same_mid_twice_is_single_observation(bi, bus):
    """同 mid 二次 observe：不落第二条事件、不改工单、不计限频、out.dup=True。"""
    first = bi.observe_group_message(
        CFG, chat_id=-100123, account_id="777", reporter_id="u1",
        reporter_name="skuio 花无缺", text="发图还是失败，报错了", msg_id=1001)
    assert first["active"] and first["category"] == "bug"
    tid = first["ticket_id"]
    n_ev = len(bi.list_events(None))
    body0 = bi.get_ticket(tid)["body"]
    again = bi.observe_group_message(
        CFG, chat_id=-100123, account_id="777", reporter_id="u1",
        reporter_name="skuio", text="发图还是失败，报错了", msg_id=1001, now=time.time() + 300)
    assert again["active"] is False and again.get("dup") is True
    assert len(bi.list_events(None)) == n_ev
    assert bi.get_ticket(tid)["body"] == body0
    assert bi.get_ticket(tid)["report_count"] == 1
    assert bi.dump_stats()["observe_dup"] == 1
    assert bi.dump_stats()["observed"] == 1


def test_observe_realtime_without_mid_then_replay_with_mid_dedups(bi, bus):
    """实时链现状不传 msg_id（skill_manager 调用点无 mid）→ 回放带 mid 在 60s 内
    按 (reporter, 文本) 近似判重；超窗 / 两侧都带不同 mid 则是真两条。"""
    t0 = 1000.0
    a = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                 reporter_id="u1", reporter_name="skuio 花无缺",
                                 text="语音发不出去", now=t0)
    assert a["active"] and a["category"] == "bug"
    b = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                 reporter_id="u1", reporter_name="skuio",
                                 text="语音发不出去", now=t0 + 30, msg_id=2001)
    assert b.get("dup") is True
    # 同文本超窗 → 不是同一条（正常观察：收集窗内 → collect 补录进同一单）
    c = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                 reporter_id="u1", reporter_name="skuio",
                                 text="语音发不出去", now=t0 + 120, msg_id=2002)
    assert c["active"] is True and not c.get("dup")
    assert c["category"] == "collect" and c["ticket_id"] == a["ticket_id"]
    # 两侧都带 mid 且不同 → 60s 内同文本也是两条真消息
    d = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                 reporter_id="u1", reporter_name="skuio",
                                 text="语音发不出去", now=t0 + 130, msg_id=2003)
    assert d["active"] is True and not d.get("dup")
    # 别的报障人同文本不判重
    e = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                 reporter_id="u9", reporter_name="钧",
                                 text="语音发不出去", now=t0 + 5)
    assert e["active"] is True


def test_verify_yes_not_retriggered_by_backfill_replay(bi, bus):
    """0904 21:0x 实录：花无缺一句「我这边显示的都好了」实时链已消费（#153），
    4 分钟后 backfill 重喂又在 #154 上 verify_yes。现在：实时链见过的 mid 回放
    直接跳过；即便没进哨兵、只要 60s 内同文本也短路——第二张单保持 fixed。
    （B/D5 后确认句须带 #N 才翻单；本例带 #tid_a，A 的「不二次观察」保证不变。）"""
    from src.ops.bug_intake_backfill import run_backfill_once, save_state
    tid_a = _mk_fixed_notified(bi, reporter="u1", text="消息发不出去")
    tid_b = _mk_fixed_notified(bi, reporter="u1", text="语音听不到")
    t0 = time.time()
    txt = f"#{tid_a} 我这边显示的都好了"
    # 实时链：哨兵登记 mid=3001（此处只登记不判洞），随后 observe（无 mid，现状）
    bi.note_group_msg_seq(CFG, -100123, 3001, account_id="777", now=t0)
    rt = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                  reporter_id="u1", reporter_name="skuio 花无缺",
                                  text=txt, now=t0)
    assert rt["category"] == "verify"
    verified = {tid_a, tid_b} - {
        t for t in (tid_a, tid_b) if bi.get_ticket(t)["status"] == "fixed"}
    assert len(verified) == 1
    n_yes = bi.dump_stats()["verify_yes"]
    # backfill 4 分钟后回放同一条（水位落后于 3001）
    sp = bi._db_path().parent / "state.json"
    save_state({"-100123": {"last_msg_id": 3000, "ts": t0}}, sp)

    async def _fetch(chat_id, cap):
        return [{"id": 3001, "text": txt, "reporter_id": "u1",
                 "reporter_name": "skuio", "outgoing": False, "has_media": False}]

    s = _run(run_backfill_once(CFG, _fetch, account_id="777", state_path=sp,
                               now=t0 + 240))
    assert s["seen_skipped"] == 1 and s["replayed"] == 0
    assert bi.dump_stats()["verify_yes"] == n_yes
    still_fixed = [t for t in (tid_a, tid_b) if bi.get_ticket(t)["status"] == "fixed"]
    assert len(still_fixed) == 1, "第二张单不得被同一句话再 verify_yes"
    assert len(bi.list_events(["verify_yes"])) == 1


def test_dump_stats_has_observe_dup(bi):
    assert "observe_dup" in bi.dump_stats()


# ── C2（2026-09-02）：值守可见性——台账事件只读口 ─────────────────────────────

def test_list_events_filters_and_default_kinds(bi):
    bi._record_event(-100123, "rate_capped_report", "u1", "语音发不出", )
    bi._record_event(-100123, "usage", "u2", "怎么切换全自动？")
    bi._record_event(-100123, "bug_new", "u3", "崩溃")
    bi._record_event(-100999, "usage", "u4", "别的群")
    evs = bi.list_events(bi.DUTY_VISIBLE_EVENT_KINDS)
    assert [e["kind"] for e in evs] == ["rate_capped_report", "usage", "usage"]
    evs2 = bi.list_events(bi.DUTY_VISIBLE_EVENT_KINDS, chat_id=-100123)
    assert [e["reporter_id"] for e in evs2] == ["u1", "u2"]
    assert bi.list_events(["usage"], since_ts=9e12) == []
    assert len(bi.list_events(None)) == 4
    assert set(bi.DUTY_VISIBLE_EVENT_KINDS) == {"rate_capped_report", "usage"}


def test_wiring_events_route_registered():
    src = _read("src/web/routes/bug_intake_routes.py")
    assert '"/api/admin/bug-intake/events"' in src
    assert "DUTY_VISIBLE_EVENT_KINDS" in src
