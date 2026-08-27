# -*- coding: utf-8 -*-
"""报障群 AI 值守门禁（bug_intake，2026-08-18）。

覆盖：配置解析 / 分类（bug 优先于 usage 的不对称语义）/ 严重度 / 危机词 /
查重归并 + 多人抬级 / 限频与收集窗三态触发 / 工单台账状态机 / 告警去抖 /
观测导出 / 四个消费点的静态接线钉（trigger 顶部三态、sender 语音压制、
skill_manager 注入+footer、ai_client 块消费、metrics 段、路由注册）。
"""
from __future__ import annotations

from pathlib import Path

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
    bug_intake.reset_state_for_tests()
    monkeypatch.setattr(
        bug_intake, "_db_path", lambda: tmp_path / "bug_intake.db")
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
    out = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u1", reporter_name="张三",
                                   text="好了，能用了")
    assert out["category"] == "verify"
    assert "✅" in out["footer"] and f"#{tid}" in out["footer"]
    assert bi.get_ticket(tid)["status"] == "verified"


def test_verify_no_reopens_and_rearms_collect(bi, bus):
    tid = _mk_fixed_notified(bi, reporter="u2")
    out = bi.observe_group_message(CFG, chat_id=-100123, account_id="777",
                                   reporter_id="u2", reporter_name="李四",
                                   text="更新了还是不行")
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
