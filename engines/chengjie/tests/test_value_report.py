# -*- coding: utf-8 -*-
"""AI 价值周报聚合门禁（src/ops/value_report.py，P1-3 首件 2026-08-06）。

钉住的不变量：
1. 持久口径聚合正确：reply_drafts 创建/处置分布/真发、outreach 触达+回复率
   （触达后 7 天窗口内首条入站才算回复、窗口外不算）、messages 出入站量——
   全部按时间窗切本周/上周，环比文本正确；
2. 不猜枚举：status/autopilot_level 原样 GROUP BY 出现在报表里；
3. 软失败：store 为 None/坏对象 → 返回 {}，绝不拖垮周报端点；
4. 接线：/api/report/weekly 与 admin 推送循环都消费同一核心（防「API 扩了、
   推送还是一句 KB 命中率」的口径分裂）。
"""
from __future__ import annotations

import inspect
import time
from pathlib import Path

from src.inbox.store import InboxStore
from src.ops.value_report import build_weekly_value, weekly_value_lines

NOW = 1_800_000_000.0
DAY = 86400.0


def _mk_store(tmp_path) -> InboxStore:
    return InboxStore(tmp_path / "inbox.db")


def _seed(store: InboxStore) -> None:
    with store._lock:
        c = store._conn
        # 本周草稿：3 创建（L1×2 / L2×1），2 处置（approved/rejected），1 真发
        rows = [
            ("d1", "conv:a", NOW - 1 * DAY, "approved", "L1", NOW - 0.5 * DAY, NOW - 0.5 * DAY),
            ("d2", "conv:a", NOW - 2 * DAY, "rejected", "L1", NOW - 1.5 * DAY, 0),
            ("d3", "conv:b", NOW - 3 * DAY, "pending", "L2", 0, 0),
            # 上周草稿：1 创建 1 处置
            ("d4", "conv:b", NOW - 9 * DAY, "approved", "L2", NOW - 8.5 * DAY, NOW - 8.5 * DAY),
        ]
        for did, conv, created, status, lvl, decided, sent in rows:
            c.execute(
                "INSERT INTO reply_drafts (draft_id, conversation_id, source_kind, "
                "source_id, status, autopilot_level, decided_at, sent_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, conv, "inbox", f"src-{did}", status, lvl, decided, sent, created, created))
        # 触达：本周 2 次（1 次窗口内获回复、1 次无回复），上周 1 次
        c.execute("INSERT INTO outreach_log (conversation_id, batch_id, status, ts) "
                  "VALUES ('conv:a', 'care:x', 'sent', ?)", (NOW - 2 * DAY,))
        c.execute("INSERT INTO outreach_log (conversation_id, batch_id, status, ts) "
                  "VALUES ('conv:b', 'proactive', 'sent', ?)", (NOW - 3 * DAY,))
        c.execute("INSERT INTO outreach_log (conversation_id, batch_id, status, ts) "
                  "VALUES ('conv:a', 'care:y', 'sent', ?)", (NOW - 10 * DAY,))
        # 消息：conv:a 触达后 1 天有入站（算回复）；conv:b 触达后 8 天才入站（窗口外不算）。
        # 出站：本周 2 条、上周 1 条。
        msgs = [
            ("m1", "conv:a", "in", NOW - 1 * DAY),
            ("m2", "conv:b", "in", NOW - 3 * DAY + 8 * DAY),   # 窗口(7天)外回复 → 不算
            ("m3", "conv:a", "out", NOW - 1 * DAY),
            ("m4", "conv:b", "out", NOW - 2 * DAY),
            ("m5", "conv:a", "out", NOW - 9 * DAY),
        ]
        for mid, conv, direction, ts in msgs:
            c.execute(
                "INSERT INTO messages (message_id, conversation_id, direction, ts, ingested_at) "
                "VALUES (?,?,?,?,?)", (mid, conv, direction, ts, ts))
        c.commit()


def test_weekly_value_aggregates(tmp_path):
    store = _mk_store(tmp_path)
    _seed(store)
    v = build_weekly_value(store, now=NOW)
    tw, lw = v["this_week"], v["last_week"]

    assert tw["drafts"]["created"] == 3
    assert tw["drafts"]["resolved_by_status"] == {"approved": 1, "rejected": 1}
    assert tw["drafts"]["created_by_level"] == {"L1": 2, "L2": 1}
    assert tw["drafts"]["sent"] == 1
    assert lw["drafts"]["created"] == 1

    # 回复率：2 触达、1 次在 7 天窗口内获回复（m2 在窗口外，m1 是 conv:a 触达前?
    # —— m1 ts=NOW-1d > 触达 ts=NOW-2d 且间隔 1d < 7d → 算回复）
    assert tw["outreach"]["sent"] == 2
    assert tw["outreach"]["responded"] == 1
    assert tw["outreach"]["response_rate"] == 50.0
    assert tw["outreach"]["by_batch_prefix"] == {"care": 1, "proactive": 1}
    assert lw["outreach"]["sent"] == 1

    assert tw["traffic"]["messages_out"] == 2
    assert lw["traffic"]["messages_out"] == 1

    # 摘要行：三段都在、环比文本存在
    text = "\n".join(v["text_lines"])
    assert "AI 拟稿 3 条" in text and "主动触达 2 次" in text and "出站消息 2 条" in text
    assert "环比" in text or "上周无数据" in text


def test_outreach_bot_excluded_consistently(tmp_path):
    """bot 会话触达在主计数与 batch 分布里必须**同时**被剔——首跑实数曾出现
    分布合计 159 > 主计数 157 的口径分裂（分布查询漏了 bot 剔除）。"""
    store = _mk_store(tmp_path)
    with store._lock:
        c = store._conn
        c.execute("INSERT INTO conversations (conversation_id, platform, peer_is_bot, "
                  "created_at, updated_at) VALUES ('conv:bot', 'telegram', 1, ?, ?)",
                  (NOW - 30 * DAY, NOW - 30 * DAY))
        c.execute("INSERT INTO outreach_log (conversation_id, batch_id, status, ts) "
                  "VALUES ('conv:bot', 'care:bot', 'sent', ?)", (NOW - 1 * DAY,))
        c.execute("INSERT INTO outreach_log (conversation_id, batch_id, status, ts) "
                  "VALUES ('conv:human', 'care:x', 'sent', ?)", (NOW - 2 * DAY,))
        c.commit()
    v = build_weekly_value(store, now=NOW)
    o = v["this_week"]["outreach"]
    assert o["sent"] == 1                       # bot 触达不计
    assert o["by_batch_prefix"] == {"care": 1}  # 分布与主计数同口径
    assert sum(o["by_batch_prefix"].values()) == o["sent"]


def test_weekly_value_soft_fail():
    assert build_weekly_value(None) == {}

    class _Broken:
        _lock = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))

    assert build_weekly_value(_Broken()) == {}


def test_weekly_value_empty_store(tmp_path):
    store = _mk_store(tmp_path)
    v = build_weekly_value(store, now=NOW)
    assert v["this_week"]["drafts"]["created"] == 0
    assert v["this_week"]["outreach"]["sent"] == 0
    assert v["this_week"]["outreach"]["response_rate"] == 0.0


def test_lines_pct_delta_semantics():
    tw = {"drafts": {"created": 4, "resolved_by_status": {}, "sent": 0},
          "outreach": {"sent": 0, "responded": 0, "response_rate": 0.0},
          "traffic": {"messages_out": 10, "messages_in": 0}}
    lw = {"drafts": {"created": 2}, "outreach": {"sent": 0},
          "traffic": {"messages_out": 20}}
    text = "\n".join(weekly_value_lines(tw, lw))
    assert "+100%" in text     # 4 vs 2
    assert "-50%" in text      # 10 vs 20


def test_goals_section_included_when_active(tmp_path):
    """P2：营销目标段——显式传 goal_store、窗口内有终态 → 出段出行；金额随行。"""
    from src.companion.goals.store import GoalStore
    store = _mk_store(tmp_path)
    gs = GoalStore(":memory:")
    g = gs.create_goal(conversation_id="telegram:a1:1", platform="telegram",
                       account_id="a1", chat_key="1",
                       template="conversion_unlock", now=NOW - 3 * DAY)
    gs.update_goal_fields(g["goal_id"], status="done", done_at=NOW - 1 * DAY,
                          result="order:pro:O1")
    import json as _json
    gs.add_event(g["goal_id"], "won_meta", _json.dumps({"amount": 199}))
    g2 = gs.create_goal(conversation_id="telegram:a1:2", platform="telegram",
                        account_id="a1", chat_key="2",
                        template="conversion_unlock", now=NOW - 12 * DAY)
    gs.update_goal_fields(g2["goal_id"], status="expired",
                          done_at=NOW - 9 * DAY)
    v = build_weekly_value(store, goal_store=gs, now=NOW)
    assert v["this_week"]["goals"] == {
        "done": 1, "failed": 0, "expired": 0, "won": 1, "won_amount": 199.0}
    assert v["last_week"]["goals"]["expired"] == 1
    text = "\n".join(v["text_lines"])
    assert "目标达成 1 个" in text and "赢单 1 单" in text and "$199" in text


def test_goals_section_absent_when_inactive(tmp_path):
    """两周皆零 → 不塞空段不出行（没在用目标的部署周报零变化）。"""
    from src.companion.goals.store import GoalStore
    store = _mk_store(tmp_path)
    v = build_weekly_value(store, goal_store=GoalStore(":memory:"), now=NOW)
    assert "goals" not in v["this_week"]
    assert not any("目标达成" in ln for ln in v["text_lines"])


def test_goals_miss_triage_in_weekly(tmp_path):
    """P4：失守三分法进周报——与失守日报/报表页同判定（offered=开价拍真发过 /
    engaged=窗口内 ≥2 条入站 / silent），行文带三分计数，且 offered≥1 时给
    「复核站外漏标」行动提示（生产 44/48 过期、0 赢单——隐藏赢单是主要读数）。"""
    from src.companion.goals.store import GoalStore
    store = _mk_store(tmp_path)
    gs = GoalStore(":memory:")

    def _mk_missed(chat: str) -> str:
        g = gs.create_goal(
            conversation_id=f"telegram:a1:{chat}", platform="telegram",
            account_id="a1", chat_key=chat, template="acquire_and_convert",
            now=NOW - 9 * DAY)
        gs.update_goal_fields(g["goal_id"], status="expired",
                              done_at=NOW - 1 * DAY)
        return str(g["goal_id"])

    gid_off = _mk_missed("c-off")
    _mk_missed("c-eng")
    _mk_missed("c-sil")
    # offered 判据＝push_level=direct 且 status=consumed 的拍真实存在
    gs.upsert_action(gid_off, "2027-01-01", kind="beat", intent="offer",
                     push_level="direct", status="consumed")
    # engaged 判据＝[start_ts, done_at] 窗口内 ≥2 条入站
    with store._lock:
        c = store._conn
        for i, ts in enumerate((NOW - 5 * DAY, NOW - 4 * DAY)):
            c.execute(
                "INSERT INTO messages (message_id, conversation_id, direction,"
                " ts, ingested_at) VALUES (?,?,?,?,?)",
                (f"tm{i}", "telegram:a1:c-eng", "in", ts, ts))
        c.commit()
    v = build_weekly_value(store, goal_store=gs, now=NOW)
    g = v["this_week"]["goals"]
    assert g["expired"] == 3
    assert g["triage"] == {"offered": 1, "engaged": 1, "silent": 1}
    text = "\n".join(v["text_lines"])
    assert "已开价 1" in text and "聊过没成 1" in text and "没聊起来 1" in text
    assert "复核" in text, "开过价的失守必须给行动提示"


def test_goals_triage_soft_degrade_without_lister(tmp_path):
    """旧 goal store（无 list_missed_window）→ 无 triage 键、行文退回纯计数
    ——getattr 特性探测防「假 store/旧进程」硬崩。"""
    store = _mk_store(tmp_path)

    class _OldGoalStore:
        def outcome_counts(self, lo, hi):
            return {"done": 0, "failed": 0, "expired": 2, "won": 0,
                    "won_amount": 0.0}

    v = build_weekly_value(store, goal_store=_OldGoalStore(), now=NOW)
    g = v["this_week"]["goals"]
    assert g["expired"] == 2 and "triage" not in g
    text = "\n".join(v["text_lines"])
    assert "未达成 2 个" in text and "已开价" not in text


def test_cases_section_included_when_active(tmp_path):
    """P3：案例趋势段——显式传 case_trend_store、窗口内有开/结案 → 出段出行。"""
    from src.utils.case_trend_store import CaseTrendStore
    store = _mk_store(tmp_path)
    cts = CaseTrendStore(":memory:")
    cts.add_opened("media_complaint", now=NOW - 2 * DAY)
    cts.add_opened("human_request", now=NOW - 1 * DAY)
    cts.add_closed(now=NOW - 1 * DAY, source="media_complaint",
                   resolution_bucket="soothed")
    cts.add_opened("ai_doubt", now=NOW - 10 * DAY)  # 上周
    v = build_weekly_value(store, case_trend_store=cts, now=NOW)
    assert v["this_week"]["cases"]["opened"] == 2
    assert v["this_week"]["cases"]["closed"] == 1
    assert v["this_week"]["cases"]["by_source"]["media_complaint"] == 1
    assert v["this_week"]["cases"]["closed_by_resolution"]["soothed"] == 1
    assert v["last_week"]["cases"]["opened"] == 1
    text = "\n".join(v["text_lines"])
    assert "案例立案 2 条" in text and "结案 1 条" in text
    cts.close()


def test_cases_section_absent_when_inactive(tmp_path):
    from src.utils.case_trend_store import CaseTrendStore
    store = _mk_store(tmp_path)
    v = build_weekly_value(store, case_trend_store=CaseTrendStore(":memory:"),
                           now=NOW)
    assert "cases" not in v["this_week"]
    assert not any("案例立案" in ln for ln in v["text_lines"])


def test_wired_into_weekly_route_and_push_loop():
    """API 与推送循环必须消费同一核心（防口径分裂）。"""
    route_src = Path("src/web/routes/report_routes.py").read_text(encoding="utf-8")
    assert "build_weekly_value" in route_src, "周报端点未接价值聚合"
    admin_src = Path("src/web/admin.py").read_text(encoding="utf-8")
    loop_seg = admin_src[admin_src.index("async def _weekly_report_loop"):]
    loop_seg = loop_seg[:loop_seg.index("async def", 10) if "async def" in loop_seg[10:] else len(loop_seg)]
    assert "build_weekly_value" in loop_seg, "周报推送循环未接价值聚合（推送仍只有 KB 一句）"
    # 第三消费面（2026-08-06）：watchdog 的 ops_report 走**新告警栈**（notify_webhooks
    # 推荐通道），F4 legacy 循环只走 config.yaml::webhook 旧栈（默认关）——运营按推荐
    # 路径接通后真正收到的是 ops_report，价值行必须也在那条链上。
    wd_src = Path("src/inbox/health_watchdog.py").read_text(encoding="utf-8")
    assert "build_weekly_value" in wd_src, "watchdog ops_report 未携带价值行（推荐通道收不到 AI 价值总账）"
