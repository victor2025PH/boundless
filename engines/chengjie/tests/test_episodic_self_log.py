"""J-10 A4（#177）：「AI 自身经历」入口——人设说过 / 承诺过 / 发过（只读 + 删一条）。

验收：人工发过「我有个女儿」后档案抽屉接口里能看到；删除只改那份 bounded 日志、
不碰任何注入逻辑（human_said_note 少那一条即可）。全部 tmp_path。
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from src.inbox import human_outbound_memory as hom
from src.utils.context_store import ContextStore, make_context_key
from src.utils.memory_self_log import (
    KIND_AI_STATE,
    KIND_HUMAN_SAID,
    KIND_MEDIA_SENT,
    KIND_PROMISE_PENDING,
    collect_self_experience,
    delete_self_experience_entry,
)


def _ctx_with_everything(now: float):
    return {
        "_human_said_log": [
            {"ts": now - 300, "fact": "我有个女儿", "author": "human", "quote": "我有个女儿，今年五岁"},
            {"ts": now - 200, "fact": "以后你可以搬过来和我一起住", "author": "human", "quote": "x"},
            "garbage",
        ],
        "_self_state_log": [{"ts": now - 100, "state": "sleep", "phrase": "我先去睡了"}],
        "_media_sent_log": [{"ts": now - 50, "note": "[图片] 刚拍的咖啡", "scene": "cafe", "series": "s1"}],
        "_media_pending": {"kind": "image", "ts": now - 30, "subject": "烤串", "source": "ai_promise"},
    }


def test_collect_orders_and_normalizes():
    now = time.time()
    items = collect_self_experience(_ctx_with_everything(now), now=now)
    kinds = [i["kind"] for i in items]
    assert kinds == [KIND_PROMISE_PENDING, KIND_MEDIA_SENT, KIND_AI_STATE, KIND_HUMAN_SAID,
                     KIND_HUMAN_SAID]
    assert items[0]["text"] == "烤串" and items[0]["media_kind"] == "image"
    assert items[1]["scene"] == "cafe"
    assert items[2]["text"] == "我先去睡了" and items[2]["state"] == "sleep"
    assert items[4]["text"] == "我有个女儿" and items[4]["author"] == "human"
    # 承诺过期（TTL 15 分钟）→ 不再显示为待兑现
    old = _ctx_with_everything(now)
    old["_media_pending"]["ts"] = now - 3600
    assert all(i["kind"] != KIND_PROMISE_PENDING for i in collect_self_experience(old, now=now))
    # 图片真发出（晚于承诺）→ 悬置熄灭
    done = _ctx_with_everything(now)
    done["_media_sent_log"][0]["ts"] = now - 10
    assert all(i["kind"] != KIND_PROMISE_PENDING for i in collect_self_experience(done, now=now))
    assert collect_self_experience(None) == [] and collect_self_experience({}) == []


def test_delete_entry_only_touches_that_log():
    now = time.time()
    ctx = _ctx_with_everything(now)
    assert delete_self_experience_entry(ctx, KIND_HUMAN_SAID, now - 300, "我有个女儿") is True
    assert [e["fact"] for e in ctx["_human_said_log"] if isinstance(e, dict)] == ["以后你可以搬过来和我一起住"]
    assert len(ctx["_self_state_log"]) == 1 and len(ctx["_media_sent_log"]) == 1
    # 文本/时间不匹配 → 不删
    assert delete_self_experience_entry(ctx, KIND_HUMAN_SAID, now - 200, "别的") is False
    assert delete_self_experience_entry(ctx, KIND_AI_STATE, now - 999, "我先去睡了") is False
    assert delete_self_experience_entry(ctx, KIND_MEDIA_SENT, now - 50, "[图片] 刚拍的咖啡") is True
    assert ctx["_media_sent_log"] == []
    assert delete_self_experience_entry(ctx, KIND_PROMISE_PENDING, 0, "") is True
    assert "_media_pending" not in ctx
    assert delete_self_experience_entry(ctx, KIND_PROMISE_PENDING, 0, "") is False
    assert delete_self_experience_entry(ctx, "nope", 0, "") is False
    assert delete_self_experience_entry(None, KIND_HUMAN_SAID, 0, "") is False


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


def test_routes_show_human_said_and_delete(tmp_path):
    """人工发过「我有个女儿」→ 抽屉接口看得到 → 删一条 → 注入块少那一条，其余不动。"""
    from starlette.testclient import TestClient
    from src.utils.audit_store import AuditStore
    from src.web.admin import create_app
    from tests.test_web_episodic_memory_api import _load_cm, _run_async

    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    sm = _FakeSM(tmp_path / "bot.db")
    res = hom.on_human_outbound(
        "whatsapp", "17345893506", "13308422244",
        "我有个女儿，以后你可以搬过来和我一起住",
        conversation_id="whatsapp:17345893506:13308422244",
        skill_manager=sm, now=1_757_000_000.0)
    assert res["ok"] and res["facts"]
    tc = MagicMock()
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        # 记忆键带平台前缀（canonical）→ 剥前缀命中 acct:peer 的上下文
        r = client.get("/api/episodic-memory/self-log",
                       params={"memory_key": "whatsapp:17345893506:13308422244"})
        assert r.status_code == 200
        d = r.json()
        assert d["found"] is True and d["context_key"] == "17345893506:13308422244"
        texts = [i["text"] for i in d["items"] if i["kind"] == KIND_HUMAN_SAID]
        assert any("我有个女儿" in t for t in texts)
        # conversation_id 直达
        r2 = client.get("/api/episodic-memory/self-log",
                        params={"conversation_id": "whatsapp:17345893506:13308422244"})
        assert r2.json()["count"] == d["count"]
        # 找不到的键：不是错误
        r3 = client.get("/api/episodic-memory/self-log", params={"memory_key": "nobody:1:2"})
        assert r3.status_code == 200 and r3.json()["found"] is False and r3.json()["items"] == []
        assert client.get("/api/episodic-memory/self-log").status_code == 400
        # 删「我有个女儿」那条
        target = next(i for i in d["items"] if i["kind"] == KIND_HUMAN_SAID and "我有个女儿" in i["text"])
        r4 = client.post("/api/episodic-memory/self-log/delete", json={
            "memory_key": "whatsapp:17345893506:13308422244", "kind": target["kind"],
            "ts": target["ts"], "text": target["text"]})
        assert r4.status_code == 200 and r4.json()["ok"] is True
        # 再删同一条 → 404；缺 kind → 400
        assert client.post("/api/episodic-memory/self-log/delete", json={
            "memory_key": "whatsapp:17345893506:13308422244", "kind": target["kind"],
            "ts": target["ts"], "text": target["text"]}).status_code == 404
        assert client.post("/api/episodic-memory/self-log/delete", json={
            "memory_key": "x"}).status_code == 400
        assert audit.query(limit=5, action="episodic_self_log_delete")
    # 注入块：少了「我有个女儿」，其余人设自述仍在；且已落盘（换实例仍如此）
    ctx = sm._get_user_context("13308422244", account_id="17345893506")
    note = hom.human_said_note(ctx)
    assert "我有个女儿" not in note
    sm._context_store.close()
    sm2 = _FakeSM(tmp_path / "bot.db")
    ctx2 = sm2._get_user_context("13308422244", account_id="17345893506")
    assert all("我有个女儿" not in str(e.get("fact")) for e in ctx2.get(hom.LOG_KEY) or [])
    sm2._context_store.close()
