"""J-8 #182：手动加关怀「意图倒置」修复门禁。

实录事故：运营把「主动问候对方早上好」填进「什么事」→ AI 把它当**客户承诺**
要问候某人，虚构第三方、还把无关记忆（人鱼潘）硬塞进话术。

覆盖：
- 意图守卫 ``detect_instruction_intent``：指令/要发的话 → 命中；客户的事 → 放行
  （含「她生日快乐那天要回老家」这类事件名词有否定优先权的边界）。
- 记忆相关性 ``filter_relevant_memory``：人鱼潘被筛掉、相关条目保留、一条不剩 → 空。
- ``build_care_prompt``：无关记忆不进 prompt；verbatim 行的派发**零 LLM 调用**、
  原文进出站队列、豁免 no_context / already_discussed；dry_run 快照原文。
- store ``add_verbatim``：不截 160、topic_norm 前缀、``is_verbatim_care`` 识别。
- 路由：add 端点守卫拦下（reason=looks_like_instruction）/ confirm_event 放行 /
  mode=verbatim 入队；preview 对 verbatim 行零 LLM 回原文 + 理解回显；
  send-text 走派发器 deliver_text 并 mark_sent。
"""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_dispatcher import CareDispatcher, build_care_prompt
from src.contacts.care_intent import (
    care_understanding,
    detect_instruction_intent,
    filter_relevant_memory,
)
from src.contacts.care_schedule import (
    VERBATIM_CARE_NORM_PREFIX,
    CareScheduleStore,
    care_verbatim_text,
    is_verbatim_care,
)

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()  # 周三 10:00（非安静时段）
MERMAID_MEMORY = (
    "- 对方喜欢人鱼潘\n"
    "- 对方下周三有考试，很紧张\n"
    "- 对方是程序员\n"
    "- 考试科目是高数"
)


# ── ① 意图守卫 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,reason", [
    ("主动问候对方早上好", "lead_verb"),   # 事故原话
    ("提醒他明天带伞", "lead_verb"),
    ("告诉客户我们周末休息", "lead_verb"),
    ("祝她生日快乐", "lead_verb"),
    ("问问他考试怎么样", "lead_verb"),
    ("greet the customer good morning", "lead_verb"),
    ("\u201c早安，今天也要加油\u201d", "quoted"),
    ("对方早上好", "target_greeting"),
])
def test_instruction_intent_hits(text, reason):
    v = detect_instruction_intent(text)
    assert v["looks_like_instruction"] is True
    assert v["reason"] == reason


@pytest.mark.parametrize("text", [
    "对方下周三考试", "下周一面试", "客户妈妈下周做手术", "对方说周五搬家",
    "她生日快乐那天要回老家",   # 对象+问候语但带事件名词 → 客户的事
    "复查", "提车", "",
])
def test_instruction_intent_passes_customer_events(text):
    assert detect_instruction_intent(text)["looks_like_instruction"] is False


# ── ② 记忆相关性 ────────────────────────────────────────────────────────────
def test_memory_filter_drops_mermaid_keeps_exam_lines():
    out = filter_relevant_memory(MERMAID_MEMORY, "下周三考试", "")
    assert "人鱼潘" not in out
    assert "程序员" not in out
    assert "考试，很紧张" in out and "高数" in out


def test_memory_filter_no_overlap_returns_empty():
    assert filter_relevant_memory(MERMAID_MEMORY, "早上好", "") == ""
    assert filter_relevant_memory("", "考试") == ""
    # 参考文本全是功能字 → 无内容词 → 宁可不注入
    assert filter_relevant_memory(MERMAID_MEMORY, "的了是", "") == ""


def test_build_care_prompt_filters_irrelevant_memory():
    item = {"topic": "下周三考试", "event_at": NOW + 86400, "due_at": NOW + 86400,
            "source_text": "下周三要考高数了"}
    p = build_care_prompt(item, context_block="x", now=NOW, memory_block=MERMAID_MEMORY)
    assert "人鱼潘" not in p
    assert "高数" in p
    # 一条相关都没有 → 记忆块整块不出现
    item2 = dict(item, topic="早上好", source_text="")
    p2 = build_care_prompt(item2, context_block="x", now=NOW, memory_block=MERMAID_MEMORY)
    assert "人鱼潘" not in p2 and "程序员" not in p2


# ── ③ store：verbatim 行 ─────────────────────────────────────────────────────
def test_add_verbatim_keeps_full_text_and_prefix():
    s = CareScheduleStore(":memory:")
    long_text = "早上好呀，" + "今天也要加油～" * 40  # > 160 字
    rid = s.add_verbatim(contact_key="tg:u1", due_at=NOW + 3600, text=long_text,
                         platform="telegram", account_id="default", chat_key="u1")
    assert rid
    it = s.get(rid)
    assert is_verbatim_care(it)
    assert str(it["topic_norm"]).startswith(VERBATIM_CARE_NORM_PREFIX)
    assert care_verbatim_text(it) == long_text          # 不截 160
    assert len(it["topic"]) <= 80                        # 摘要给卡片显示
    assert it["confidence"] == 1.0 and it["status"] == "pending"
    assert s.add_verbatim(contact_key="tg:u1", due_at=NOW + 3600, text="   ") is None
    # 普通行不是 verbatim
    c = CareCommitment(due_at=NOW + 3600, event_at=NOW + 3600, topic="面试",
                       sentiment="neutral", anchor_text="x", source_text="明天面试", confidence=0.9)
    rid2 = s.add_commitment(c, contact_key="tg:u2", platform="telegram",
                            account_id="default", chat_key="u2")
    assert not is_verbatim_care(s.get(rid2)) and care_verbatim_text(s.get(rid2)) == ""


# ── ④ 派发器：verbatim 零 LLM ─────────────────────────────────────────────────
class _AI:
    def __init__(self):
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return "AI 拟的稿子"


def _sender(record, row_id=321):
    async def _send(channel, account_id, chat_name, reply, defer_until, reason, staleness, extra):
        record.append({"channel": channel, "chat_name": chat_name, "reply": reply,
                       "reason": reason, "extra": extra, "defer_until": defer_until})
        return row_id
    return _send


def _verbatim_store(text="早上好呀，今天也要加油～"):
    s = CareScheduleStore(":memory:")
    s.add_verbatim(contact_key="tg:u1", due_at=NOW - 60, text=text,
                   platform="telegram", account_id="default", chat_key="u1")
    return s


async def test_verbatim_dispatch_skips_llm_and_sends_raw_text():
    s = _verbatim_store()
    rec, ai = [], _AI()
    # 刻意开满守卫：无上下文 skip + already_discussed 恒真——verbatim 都要豁免
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec),
                       context_provider=lambda ck: "",
                       already_discussed=lambda ck, t: True,
                       skip_if_no_context=True)
    n = await d.run_once(now=NOW)
    assert n == 1
    assert ai.prompts == []                                  # 零 LLM
    assert len(rec) == 1
    assert rec[0]["reply"] == "早上好呀，今天也要加油～"
    assert rec[0]["reason"] == "care:verbatim"
    assert rec[0]["extra"]["verbatim"] is True
    it = s.list_recent(limit=5)[0] if hasattr(s, "list_recent") else s.get(1)
    assert it["status"] == "sent"
    assert it["sent_text"] == "早上好呀，今天也要加油～"


async def test_verbatim_dispatch_dry_run_snapshots_raw_text_without_llm():
    s = _verbatim_store()
    rec, ai = [], _AI()
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec), dry_run=True)
    n = await d.run_once(now=NOW)
    assert n == 1 and rec == [] and ai.prompts == []
    it = s.get(1)
    assert it["status"] == "pending"                          # dry 不消费待办
    assert (it.get("sent_text") or "") == "早上好呀，今天也要加油～"


async def test_deliver_text_bypasses_llm_and_marks_sent():
    s = CareScheduleStore(":memory:")
    c = CareCommitment(due_at=NOW + 3600, event_at=NOW + 3600, topic="面试",
                       sentiment="neutral", anchor_text="x", source_text="明天面试", confidence=0.9)
    rid = s.add_commitment(c, contact_key="tg:u2", platform="telegram",
                           account_id="default", chat_key="u2")
    rec, ai, hooks = [], _AI(), []
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec, row_id=77),
                       sent_hook=lambda it: hooks.append(it))
    res = await d.deliver_text(s.get(rid), "面试加油！结束了跟我说说～", now=NOW)
    assert res["ok"] is True and res["row_id"] == 77
    assert ai.prompts == []
    assert rec[0]["reply"] == "面试加油！结束了跟我说说～"
    assert rec[0]["reason"] == "care:manual" and rec[0]["extra"]["manual_rewrite"] is True
    assert len(hooks) == 1
    it = s.get(rid)
    assert it["status"] == "sent" and it["sent_text"] == "面试加油！结束了跟我说说～"
    assert str(it.get("note") or "").startswith("manual:deferred:77")
    # 空文本 / 缺路由 如实拒绝，不抛
    assert (await d.deliver_text(s.get(rid), "   "))["reason"] == "empty_text"
    assert (await d.deliver_text({"id": 9, "chat_key": "", "platform": ""}, "x"))["reason"] == "missing_route"


# ── ⑤ 理解回显 ──────────────────────────────────────────────────────────────
def test_care_understanding_event_vs_verbatim():
    ev = care_understanding({"topic": "主动问候对方早上好", "event_at": NOW + 2 * 86400,
                             "due_at": NOW + 2 * 86400, "source_text": ""}, now=NOW)
    assert ev["mode"] == "event" and ev["looks_like_instruction"] is True
    assert ev["when"]  # 有人话时间词
    vb = care_understanding({"topic": "早安", "topic_norm": "verbatim:abcd1234",
                             "due_at": NOW + 3600, "source_text": "早安，今天也要加油"}, now=NOW)
    assert vb["mode"] == "verbatim" and vb["source_text"] == "早安，今天也要加油"
    assert vb["looks_like_instruction"] is False


# ── ⑥ 路由端到端 ────────────────────────────────────────────────────────────
class _Disp:
    """最小派发器桩：只暴露路由用到的两个公开方法。"""
    def __init__(self, store):
        self.store = store
        self.calls = []

    def prompt_extras(self, item):
        return {"memory_block": MERMAID_MEMORY}

    async def deliver_text(self, item, text, *, now=None):
        self.calls.append((int(item["id"]), text))
        self.store.mark_sent(int(item["id"]), note="manual:deferred:5", sent_text=text)
        return {"ok": True, "reason": "", "row_id": 5}


@pytest.fixture
def client():
    from fastapi import Request

    from src.web.routes.care_routes import register_care_routes

    app = FastAPI()
    store = CareScheduleStore(":memory:")
    app.state.care_schedule_store = store
    app.state.ai_client = _AI()
    app.state.care_engine = {"dispatcher": _Disp(store)}

    def _auth(request: Request):
        return True

    register_care_routes(app, api_auth=_auth, config_manager=None)
    return TestClient(app), app


def _post(c, url, body):
    return c.post(url, json=body).json()


def test_route_add_guard_blocks_instruction_then_confirm_passes(client):
    c, app = client
    base = {"contact_key": "tg:u1", "platform": "telegram", "account_id": "default",
            "chat_key": "u1", "due_in_hours": 24}
    r = _post(c, "/api/care/schedule", dict(base, topic="主动问候对方早上好"))
    assert r["ok"] is False and r["reason"] == "looks_like_instruction"
    assert r["suggest_mode"] == "verbatim"
    assert app.state.care_schedule_store.count(status="pending") == 0
    # 运营坚持「就是客户的事」→ confirm_event 放行
    r2 = _post(c, "/api/care/schedule", dict(base, topic="主动问候对方早上好", confirm_event=True))
    assert r2["ok"] is True and r2["mode"] == "event"
    # 正常客户事件不受影响
    r3 = _post(c, "/api/care/schedule", dict(base, topic="下周三考试"))
    assert r3["ok"] is True and r3["mode"] == "event"
    assert _post(c, "/api/care/schedule", dict(base, topic="x", mode="weird"))["reason"] == "bad_mode"


def test_route_add_verbatim_then_preview_is_raw_text_without_llm(client):
    c, app = client
    r = _post(c, "/api/care/schedule", {
        "contact_key": "tg:u1", "platform": "telegram", "account_id": "default",
        "chat_key": "u1", "due_in_hours": 2, "mode": "verbatim",
        "topic": "早上好呀，今天也要加油～",
    })
    assert r["ok"] is True and r["mode"] == "verbatim"
    it = app.state.care_schedule_store.get(r["id"])
    assert is_verbatim_care(it)
    ai = app.state.ai_client
    p = _post(c, f"/api/care/schedule/{r['id']}/preview", {})
    assert p["ok"] is True and p["mode"] == "verbatim"
    assert p["preview"] == "早上好呀，今天也要加油～"
    assert p["understanding"]["mode"] == "verbatim"
    assert ai.prompts == []                                   # 预览零 LLM


def test_route_preview_event_has_understanding_and_filtered_memory(client):
    c, app = client
    r = _post(c, "/api/care/schedule", {
        "contact_key": "tg:u1", "platform": "telegram", "account_id": "default",
        "chat_key": "u1", "due_in_hours": 2, "topic": "下周三考试",
    })
    p = _post(c, f"/api/care/schedule/{r['id']}/preview", {})
    assert p["ok"] is True and p["mode"] == "event"
    assert p["understanding"]["mode"] == "event"
    assert p["understanding"]["topic"] == "下周三考试"
    prompt = app.state.ai_client.prompts[-1]
    assert "人鱼潘" not in prompt and "高数" in prompt


def test_route_send_text_uses_dispatcher_and_marks_sent(client):
    c, app = client
    r = _post(c, "/api/care/schedule", {
        "contact_key": "tg:u1", "platform": "telegram", "account_id": "default",
        "chat_key": "u1", "due_in_hours": 2, "topic": "下周三考试",
    })
    sid = r["id"]
    assert _post(c, f"/api/care/schedule/{sid}/send-text", {"text": ""})["reason"] == "empty_text"
    res = _post(c, f"/api/care/schedule/{sid}/send-text", {"text": "考试加油～"})
    assert res["ok"] is True and res["row_id"] == 5 and res["id"] == sid
    disp = app.state.care_engine["dispatcher"]
    assert disp.calls == [(sid, "考试加油～")]
    assert app.state.care_schedule_store.get(sid)["status"] == "sent"
    # 已发行再发 → not_pending
    assert _post(c, f"/api/care/schedule/{sid}/send-text", {"text": "x"})["reason"] == "not_pending"


def test_route_intent_check(client):
    c, _ = client
    assert _post(c, "/api/care/intent-check", {"topic": "主动问候对方早上好"})["looks_like_instruction"] is True
    assert _post(c, "/api/care/intent-check", {"topic": "下周三考试"})["looks_like_instruction"] is False
