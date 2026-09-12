# -*- coding: utf-8 -*-
"""接力记忆 P0-3：人工接管 → 切回 AI 时的接力摘要（handoff_memory，2026-09-12）。"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from src.inbox import handoff_memory as hf
from src.inbox import human_outbound_memory as hom
from src.utils.context_store import ContextStore, make_context_key


class _FakeSM:
    def __init__(self, db: Path):
        self._context_store = ContextStore(db_path=db, ttl_days=30)

    def _get_user_context(self, user_id, account_id="", chat_scope=""):
        key = make_context_key(user_id, account_id)
        ctx = self._context_store.get(key)
        ctx["user_id"] = str(user_id)
        ctx["_context_store_key"] = key
        return ctx

    def _push_recent_reply(self, ctx, reply):
        ctx.setdefault("recent_replies", []).append(reply)


def _rows():
    t = 1_757_000_000.0
    return [
        {"direction": "in", "text": "你在干嘛", "ts": t - 5000},                 # 窗外
        {"direction": "out", "text": "AI 之前的回复", "ts": t - 4900},          # 窗外
        {"direction": "in", "text": "发张照片看看", "ts": t + 10},
        {"direction": "out", "text": "刚拍的\n[图片内容] 一位女性在海边微笑", "media_type": "image",
         "media_ref": "/o.jpg", "ts": t + 20},
        {"direction": "out", "text": "我有个女儿，五岁了", "ts": t + 30},
        {"direction": "in", "text": "", "media_type": "image", "media_ref": "/i.jpg", "ts": t + 40},
        {"direction": "in", "text": "[图片内容] 一只橘猫趴在沙发上", "media_type": "image",
         "media_ref": "/i2.jpg", "ts": t + 45},
        {"direction": "in", "text": "[语音]", "media_type": "voice", "media_ref": "/v.ogg", "ts": t + 50},
        {"direction": "out", "text": "晚安呀", "media_type": "voice", "media_ref": "/vo.ogg", "ts": t + 60},
        {"direction": "in", "text": "好可爱！明天见", "ts": t + 70},
    ]


def test_build_handoff_note_formats_window_only():
    t = 1_757_000_000.0
    b = hf.build_handoff_note(_rows(), since_ts=t, now=t + 100)
    note = b["note"]
    assert b["out_n"] == 3 and b["in_n"] == 5 and b["media_n"] == 5
    assert "人工接管" in note and "不要重新打招呼" in note
    assert "你在干嘛" not in note and "AI 之前的回复" not in note           # 窗外不进
    assert "- 发了图片：刚拍的（画面：一位女性在海边微笑）" in note
    assert "- 我有个女儿，五岁了" in note
    assert "- （语音）晚安呀" in note
    assert "- 发了图片（画面：一只橘猫趴在沙发上）" in note
    assert "- 发了图片\n" in note or note.endswith("- 发了图片")       # 未识别的图只记事实
    assert "- 发了一条语音（未转写）" in note
    assert "- 好可爱！明天见" in note
    assert note.index("你说过/发过：") < note.index("对方说过/发过：")


def test_build_handoff_note_empty_and_caps():
    t = 1_757_000_000.0
    assert hf.build_handoff_note([], since_ts=t, now=t)["note"] == ""
    # 只有占位/空文本 → 不值一提
    assert hf.build_handoff_note(
        [{"direction": "in", "text": "[图片]", "ts": t + 1}], since_ts=t, now=t + 2)["note"] == ""
    # 超长窗口被夹到 MAX_WINDOW_SEC；行数封顶
    rows = [{"direction": "out", "text": f"第{i}句话" + "很长" * 60, "ts": t + i} for i in range(30)]
    b = hf.build_handoff_note(rows, since_ts=1.0, now=t + 100)
    assert b["since"] >= t + 100 - hf.MAX_WINDOW_SEC
    assert b["note"].count("\n- ") == hf.MAX_OUT_LINES
    assert len(b["note"]) <= hf.NOTE_MAX_CHARS
    assert "第29句话" in b["note"] and "第0句话" not in b["note"]         # 留最近的


def test_handoff_note_consumption_uses_and_ttl():
    ctx = {hf.NOTE_KEY: {"ts": 1000.0, "note": "N", "used": 0}}
    for i in range(hf.MAX_USES):
        assert hf.handoff_note(ctx, now=1001.0) == "N"
    assert hf.handoff_note(ctx, now=1001.0) == "" and hf.NOTE_KEY not in ctx
    ctx = {hf.NOTE_KEY: {"ts": 1000.0, "note": "N", "used": 0}}
    assert hf.handoff_note(ctx, now=1000.0 + hf.TTL_SEC + 1) == "" and hf.NOTE_KEY not in ctx
    assert hf.handoff_note({}, now=1.0) == "" and hf.handoff_note(None, now=1.0) == ""


def test_record_handoff_end_to_end_with_manual_send_window(tmp_path):
    from src.inbox.models import InboxMessage
    from src.inbox.store import InboxStore

    store = InboxStore(tmp_path / "inbox.db")
    sm = _FakeSM(tmp_path / "bot.db")
    st = SimpleNamespace(skill_manager=sm, inbox_store=store)
    cid = "whatsapp:17345893506:13308422244"
    t = 1_757_000_000.0

    def _ing(mid, direction, text, ts, **kw):
        store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id=mid,
                                          direction=direction, text=text, ts=ts, **kw))

    _ing("a1", "out", "AI 老早说的", t - 3600)
    # 坐席接管：手动发一条文本 + 一张图（记忆钩子记下接管起点）
    _ing("h1", "out", "我有个女儿", t + 10)
    hom.on_human_outbound("whatsapp", "17345893506", "13308422244", "我有个女儿",
                          conversation_id=cid, skill_manager=sm, now=t + 10)
    _ing("h2", "out", "刚拍的", t + 20, media_type="image", media_ref="/static/o.jpg")
    hom.on_human_outbound("whatsapp", "17345893506", "13308422244", "刚拍的", media_type="image",
                          media_ref="/static/o.jpg", conversation_id=cid, skill_manager=sm, now=t + 20)
    _ing("c1", "in", "好看！", t + 30)
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert ctx[hf.TAKEOVER_TS_KEY] == t + 10

    # 切回全自动（前档 manual，updated_at 是最后一次出站 → 起点取更早的首条人工出站）
    res = hf.record_handoff(st, store, cid, prev_meta={"mode": "manual", "source": "takeover",
                                                        "updated_at": t + 20},
                            new_mode="auto_ai", by="mode_select", now=t + 60)
    assert res["ok"] and res["out_n"] == 2 and res["in_n"] == 1 and res["media_n"] == 1
    rec = ctx[hf.NOTE_KEY]
    assert rec["used"] == 0 and rec["by"] == "mode_select"
    assert "AI 老早说的" not in rec["note"]
    assert "- 我有个女儿" in rec["note"] and "- 发了图片：刚拍的" in rec["note"] and "- 好看！" in rec["note"]
    assert hf.TAKEOVER_TS_KEY not in ctx                              # 起点用完即清
    # 持久：重开仍在，且注入一次计一次
    sm._context_store.close()
    sm2 = _FakeSM(tmp_path / "bot.db")
    ctx2 = sm2._get_user_context("13308422244", account_id="17345893506")
    assert hf.handoff_note(ctx2, now=t + 61).startswith("【刚结束的一段人工接管")
    assert ctx2[hf.NOTE_KEY]["used"] == 1
    sm2._context_store.close()
    store.close()


def test_record_handoff_skips_when_no_window_or_manual(tmp_path):
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    sm = _FakeSM(tmp_path / "bot.db")
    st = SimpleNamespace(skill_manager=sm, inbox_store=store)
    cid = "whatsapp:17345893506:13308422244"
    assert hf.record_handoff(st, store, cid, new_mode="manual")["reason"] == "still_manual"
    # 前档 auto_ai（重选全自动）且无人工出站起点 → 无窗口，不写 note
    r = hf.record_handoff(st, store, cid, prev_meta={"mode": "auto_ai", "updated_at": 1.0},
                          new_mode="auto_ai")
    assert r["ok"] is False and r["reason"] == "no_window"
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert hf.NOTE_KEY not in ctx
    # 前档 manual（下拉切的，无人工出站）但窗口内没消息 → empty_window
    r2 = hf.record_handoff(st, store, cid, prev_meta={"mode": "manual", "updated_at": 5.0},
                           new_mode="review", now=10.0)
    assert r2["reason"] == "empty_window"
    # 无 skill_manager → 软失败
    assert hf.record_handoff(SimpleNamespace(), store, cid, new_mode="auto_ai")["reason"] == "no_skill_manager"
    assert hf.record_handoff(st, None, cid, new_mode="auto_ai")["reason"] == "no_store"
    sm._context_store.close()
    store.close()


def test_sweep_rearm_invokes_on_restored_callback():
    from src.inbox.takeover_rearm import sweep_takeover_rearm

    class _Store:
        def __init__(self):
            self.set = []

        def list_takeover_manual(self, *, before_ts, limit=500):
            return [{"conversation_id": "tg:a:1", "mode": "manual",
                     "source": "takeover_from:auto_ai", "updated_at": 100.0},
                    {"conversation_id": "tg:a:2", "mode": "manual",
                     "source": "takeover", "updated_at": 100.0}]

        def set_automation_mode(self, cid, mode, source=""):
            if cid.endswith(":2"):
                raise RuntimeError("boom")
            self.set.append((cid, mode, source))

    got = []
    st = _Store()
    res = sweep_takeover_rearm(
        st, {"inbox": {"takeover_rearm": {"enabled": True, "after_minutes": 1}}},
        now=10_000.0, on_restored=lambda cid, row, target: got.append((cid, row["updated_at"], target)))
    assert res["restored"] == 1 and got == [("tg:a:1", 100.0, "auto_ai")]

    # 回调抛异常不影响恢复计数
    res2 = sweep_takeover_rearm(
        st, {"inbox": {"takeover_rearm": {"enabled": True, "after_minutes": 1}}},
        now=10_000.0, on_restored=lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert res2["restored"] == 1


def test_window_lift_helpers_only_while_note_active(tmp_path):
    t = 1_757_000_000.0
    ctx = {hf.NOTE_KEY: {"ts": t, "note": "N", "used": 0, "since": t - 600}}
    assert hf.active_window_since(ctx, now=t + 1) == t - 600
    assert ctx[hf.NOTE_KEY]["used"] == 0                       # 只读，不计次
    hist = [{"role": "user", "content": "x", "ts": t - 700}] + [
        {"role": "assistant" if i % 2 else "user", "content": f"m{i}", "ts": t - 590 + i}
        for i in range(25)]
    assert hf.verbatim_keep_for_handoff(hist, ctx, 10, now=t + 1) == 27       # 25 条 + 2 余量
    assert hf.verbatim_keep_for_handoff(hist[:5], ctx, 10, now=t + 1) == 10   # 不足 base 不动
    assert hf.verbatim_keep_for_handoff(hist * 3, ctx, 10, now=t + 1) == hf.HANDOFF_VERBATIM_CAP
    # 过期 / 用尽 → 全部回到原值
    ctx[hf.NOTE_KEY]["used"] = hf.MAX_USES
    assert hf.active_window_since(ctx, now=t + 1) == 0.0
    assert hf.verbatim_keep_for_handoff(hist, ctx, 10, now=t + 1) == 10
    assert hf.verbatim_keep_for_handoff(hist, {}, 10) == 10

    sm = _FakeSM(tmp_path / "bot.db")
    st = SimpleNamespace(skill_manager=sm)
    kw = dict(account_id="17345893506", chat_key="13308422244",
              conversation_id="whatsapp:17345893506:13308422244", now=t + 1)
    assert hf.fetch_limit_for_handoff(st, 30, **kw) == 30
    c = sm._get_user_context("13308422244", account_id="17345893506")
    c[hf.NOTE_KEY] = {"ts": t, "note": "N", "used": 0, "since": t - 600}
    assert hf.fetch_limit_for_handoff(st, 30, **kw) == hf.HANDOFF_FETCH_MIN
    assert hf.fetch_limit_for_handoff(st, 200, **kw) == 200
    assert hf.fetch_limit_for_handoff(SimpleNamespace(), 30, **kw) == 30
    sm._context_store.close()


def test_wiring_inject_self_state_and_watchdog_and_route():
    root = Path(__file__).resolve().parents[1] / "src"
    sm_src = (root / "skills" / "skill_manager.py").read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"def _inject_self_state\(.*?\n    def ", sm_src, re.S)
    assert m and "handoff_note" in m.group(0)
    wd = (root / "inbox" / "health_watchdog.py").read_text(encoding="utf-8", errors="ignore")
    assert "on_restored=_on_restored" in wd and "record_handoff" in wd
    rt = (root / "web" / "routes" / "unified_inbox_stored_read_routes.py").read_text(
        encoding="utf-8", errors="ignore")
    assert "record_handoff(" in rt and '"handoff": handoff' in rt
    hm = (root / "inbox" / "human_outbound_memory.py").read_text(encoding="utf-8", errors="ignore")
    assert "note_human_outbound_started" in hm
