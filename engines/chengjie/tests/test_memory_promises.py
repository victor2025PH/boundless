"""J-10 二期（#177 / #171）：人设承诺账本——抓取口径 / 去重 / 状态（自动结清·超期）/ 已兑现·删除 /
两个出站捕获点接线 / 抽屉接口与 mark-done 路由。全部 tmp_path，零 LLM。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import MagicMock

from src.inbox import human_outbound_memory as hom
from src.utils.context_store import ContextStore, make_context_key
from src.utils.memory_promises import (
    KIND_CALL,
    KIND_MEDIA,
    KIND_MEET,
    LOG_KEY,
    STATUS_DONE,
    STATUS_OPEN,
    STATUS_OVERDUE,
    delete_promise,
    extract_promises,
    mark_promise_done,
    promise_entries,
    promise_note,
    record_promises,
)
from src.utils.memory_self_log import collect_self_experience, delete_self_experience_entry


# ── 抓取口径：宁漏勿误 ─────────────────────────────────────────────────────
def test_extract_first_person_future_commitments():
    got = {p["text"]: p for p in extract_promises("好呀，明天给你打电话～ 等我拍一张给你看。下次带你去吃那家火锅")}
    assert "好呀，明天给你打电话" in got and got["好呀，明天给你打电话"]["kind"] == KIND_CALL
    assert got["好呀，明天给你打电话"]["due_hint"] == "明天"
    assert got["等我拍一张给你看"]["kind"] == KIND_MEDIA
    assert got["下次带你去吃那家火锅"]["kind"] == KIND_MEET
    en = extract_promises("I will call you tomorrow. I'll send you the photo tonight!")
    assert [p["kind"] for p in en] == [KIND_CALL, KIND_MEDIA]
    assert extract_promises("我答应你，周末一定陪你去")[0]["kind"] == KIND_MEET
    assert extract_promises("改天拍给你看")[0]["kind"] == KIND_MEDIA


def test_extract_excludes_questions_negations_reported_past_and_forever():
    for t in ("明天去看你好吗？", "明天去看你好吗", "我不会给你打电话的", "要是有空我就去看你",
              "昨天已经发你了呀", "你明天给我发照片吧", "You said you would call me",
              "I can't come tomorrow", "我会一直陪着你的", "I will always be there for you",
              "我先去睡了，晚安", "哈哈那我找找看", "我有个女儿，以后你可以搬过来和我一起住", "", "x" * 5000):
        assert extract_promises(t) == [], t


# ── 记账 / 去重 / 状态 ─────────────────────────────────────────────────────
def test_record_dedupe_and_status_lifecycle():
    now = time.time()
    ctx: dict = {}
    assert record_promises(ctx, "明天给你打电话～", author="ai", now=now) == 1
    assert record_promises(ctx, "明天给你打电话～", author="ai", now=now + 3600) == 0   # 24h 内同文去重
    assert record_promises(ctx, "明天给你打电话～", author="ai", now=now + 2 * 86400) == 1
    assert record_promises(ctx, "等我拍一张给你看", author="human", now=now) == 1
    assert record_promises(ctx, "我不会给你打电话的", now=now) == 0
    assert record_promises(None, "明天给你打电话", now=now) == 0
    ents = promise_entries(ctx, now=now + 2 * 86400)
    assert len(ents) == 3 and all(e["kind"] == "promise" for e in ents)
    by_text = {}
    for e in ents:
        by_text.setdefault(e["text"], []).append(e)
    assert by_text["等我拍一张给你看"][0]["author"] == "human"
    assert by_text["等我拍一张给你看"][0]["status"] == STATUS_OPEN
    # 媒体承诺：晚于承诺的真发记录 → 自动结清
    ctx["_media_sent_log"] = [{"ts": now + 60, "note": "[图片] 刚拍的"}]
    ents = promise_entries(ctx, now=now + 2 * 86400)
    assert {e["text"]: e["status"] for e in ents}["等我拍一张给你看"] == STATUS_DONE
    # 电话承诺：第 2 天看，最早那条 open、7 天没动静 → overdue；坐席点已兑现 → done；再点 → False
    calls = {e["ts"]: e for e in ents if e["text"] == "明天给你打电话"}
    first_ts = min(calls)
    assert calls[first_ts]["status"] == STATUS_OPEN
    later = {e["ts"]: e for e in promise_entries(ctx, now=now + 9 * 86400) if e["text"] == "明天给你打电话"}
    assert later[first_ts]["status"] == STATUS_OVERDUE          # 9 天前 → 超期
    assert later[now + 2 * 86400]["status"] == STATUS_OPEN      # 恰好 7 天，还没过线
    assert mark_promise_done(ctx, first_ts, "明天给你打电话", now=now + 100) is True
    assert mark_promise_done(ctx, first_ts, "明天给你打电话") is False
    assert {e["ts"]: e["status"] for e in promise_entries(ctx, now=now + 9 * 86400)}[first_ts] == STATUS_DONE
    # 删除
    assert delete_promise(ctx, first_ts, "明天给你打电话") is True
    assert delete_promise(ctx, first_ts, "明天给你打电话") is False
    assert len(ctx[LOG_KEY]) == 2
    # 注入块（本期不接线）：只列未兑现（剩下的那条电话承诺）
    note = promise_note(ctx, now=now + 2 * 86400)
    assert "明天给你打电话" in note and "等我拍一张给你看" not in note
    assert promise_note({}) == ""


def test_promise_note_windows_and_tone():
    now = time.time()
    ctx: dict = {}
    record_promises(ctx, "明天给你打电话", now=now - 3 * 86400)          # 3 天前 → open
    record_promises(ctx, "改天拍给你看", now=now - 9 * 86400)            # 9 天前 → overdue，媒体类
    record_promises(ctx, "下次带你去吃火锅", now=now - 30 * 86400)        # 30 天前 → 超出 14 天窗，不提
    note = promise_note(ctx, now=now)
    assert "明天给你打电话" in note and "改天拍给你看" in note and "火锅" not in note
    assert "说了 9 天还没做" in note and "别自相矛盾" in note and "不要每轮解释或道歉" in note
    assert "照片/语音类" in note                                          # 有媒体承诺才加这句
    assert "照片/语音类" not in promise_note({"_promise_log": ctx["_promise_log"][:1]}, now=now)
    # max_items / max_age_days 可调；全 done → 空
    assert note.count("你说过") == 2
    assert promise_note(ctx, now=now, max_items=1).count("你说过") == 1
    assert "火锅" in promise_note(ctx, now=now, max_age_days=0)          # 0 = 不限
    for e in ctx["_promise_log"]:
        mark_promise_done(ctx, e["ts"], e["text"], now=now)
    assert promise_note(ctx, now=now) == ""


def test_inject_self_state_wires_promise_note_behind_flag():
    """三期接线：memory.promises.inject 出厂关 → 块里没有；开 → 与 human_said / self_state 同一注入口。"""
    from src.skills.skill_manager import SkillManager

    class _Stub:
        logger = logging.getLogger("t")
        _memory_cfg: dict = {}
        _inject_self_state = SkillManager._inject_self_state

    now = time.time()
    ctx: dict = {"_human_said_log": [{"ts": now - 60, "fact": "我有个女儿", "author": "human", "quote": "x"}]}
    record_promises(ctx, "明天给你打电话", now=now - 3600)
    s = _Stub()
    s._inject_self_state(ctx)
    assert "我有个女儿" in ctx["_self_state_block"] and "明天给你打电话" not in ctx["_self_state_block"]
    s._memory_cfg = {"promises": {"inject": True, "max_items": 3}}
    s._inject_self_state(ctx)
    blk = ctx["_self_state_block"]
    assert "我有个女儿" in blk and "明天给你打电话" in blk and "别自相矛盾" in blk
    # 承诺已兑现 → 块里退出；其余不受影响
    mark_promise_done(ctx, ctx[LOG_KEY][0]["ts"], "明天给你打电话")
    s._inject_self_state(ctx)
    assert "明天给你打电话" not in ctx["_self_state_block"] and "我有个女儿" in ctx["_self_state_block"]


def test_bounded_log():
    ctx: dict = {}
    for i in range(20):
        record_promises(ctx, f"明天给你打电话{i}", now=1_000_000 + i * 90000)
    assert len(ctx[LOG_KEY]) == 12


# ── 汇进「人设说过 / 承诺过」+ 两个出站捕获点 ─────────────────────────────
def test_self_experience_includes_promises_and_delete_delegates():
    now = time.time()
    ctx: dict = {"_human_said_log": [{"ts": now - 10, "fact": "我有个女儿", "author": "human"}]}
    record_promises(ctx, "明天给你打电话～", now=now - 5)
    items = collect_self_experience(ctx, now=now)
    assert [i["kind"] for i in items] == ["promise", "human_said"]
    assert items[0]["status"] == STATUS_OPEN and items[0]["promise_kind"] == KIND_CALL
    assert delete_self_experience_entry(ctx, "promise", items[0]["ts"], items[0]["text"]) is True
    assert collect_self_experience(ctx, now=now)[0]["kind"] == "human_said"


def test_skill_manager_update_after_reply_records_promise():
    """出站单点：_update_after_reply 里 record_self_state 旁边一行。绑真方法 + 最小假体。"""
    from src.skills.skill_manager import SkillManager
    import inspect
    src = inspect.getsource(SkillManager._update_after_reply)
    assert "record_promises(user_context, reply" in src, "承诺账本捕获点丢失"
    assert "record_self_state(user_context, reply)" in src


class _FakeSM:
    def __init__(self, db: Path):
        self._context_store = ContextStore(db_path=db, ttl_days=30)

    def _get_user_context(self, user_id, account_id="", chat_scope=""):
        key = make_context_key(user_id, account_id)
        ctx = self._context_store.get(key)
        ctx["user_id"] = str(user_id)
        ctx["_context_store_key"] = key
        return ctx

    def _push_recent_reply(self, user_context, reply):
        user_context.setdefault("recent_replies", []).append(reply)


def test_human_outbound_records_promise_and_route_mark_done(tmp_path):
    from starlette.testclient import TestClient
    from src.utils.audit_store import AuditStore
    from src.web.admin import create_app
    from tests.test_web_episodic_memory_api import _load_cm, _run_async

    sm = _FakeSM(tmp_path / "bot.db")
    res = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244", "好呀，明天给你打电话～",
        conversation_id="whatsapp:17345893506:13308422244", skill_manager=sm, now=1_757_000_000.0)
    assert res["ok"]
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert ctx[LOG_KEY][0]["author"] == "human" and ctx[LOG_KEY][0]["kind"] == KIND_CALL
    # 落盘：换实例仍在
    sm._context_store.close()
    sm = _FakeSM(tmp_path / "bot.db")
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    assert ctx[LOG_KEY][0]["text"] == "好呀，明天给你打电话"   # 尾部「～」当句末符剥掉

    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    tc = MagicMock()
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        d = client.get("/api/episodic-memory/self-log",
                       params={"conversation_id": "whatsapp:17345893506:13308422244"}).json()
        pr = [i for i in d["items"] if i["kind"] == "promise"]
        assert len(pr) == 1 and pr[0]["status"] == STATUS_OVERDUE   # 1_757_000_000 早于现在 7 天以上
        body = {"conversation_id": "whatsapp:17345893506:13308422244", "kind": "promise",
                "ts": pr[0]["ts"], "text": pr[0]["text"]}
        r = client.post("/api/episodic-memory/self-log/mark-done", json=body)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert client.post("/api/episodic-memory/self-log/mark-done", json=body).status_code == 404
        assert client.post("/api/episodic-memory/self-log/mark-done", json={"kind": "promise"}).status_code == 400
        d2 = client.get("/api/episodic-memory/self-log",
                        params={"conversation_id": "whatsapp:17345893506:13308422244"}).json()
        assert [i for i in d2["items"] if i["kind"] == "promise"][0]["status"] == STATUS_DONE
        assert audit.query(limit=5, action="episodic_promise_done")
    sm._context_store.close()
