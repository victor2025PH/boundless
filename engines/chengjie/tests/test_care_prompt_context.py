"""实施84 P0-2：care 拟稿上下文补齐门禁。

覆盖：增强块纯函数（全空零变化/各节渲染）、派发器透传 extras、危机专线
不吃增强、provider 异常不阻断派发、preview 同源方法、以及 background_tasks
接线的静态 ratchet（provider/already_discussed/user_clock/reactivation
sent_hook 断线即红——接线是静默的，静态钉住比事后翻日志可靠）。
"""
from datetime import datetime
from pathlib import Path

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_dispatcher import (
    CareDispatcher,
    _compose_extra_blocks,
    build_care_prompt,
)
from src.contacts.care_schedule import CareScheduleStore

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()


def _store_with(topic="面试"):
    s = CareScheduleStore(":memory:")
    c = CareCommitment(due_at=NOW - 60, event_at=NOW - 60, topic=topic,
                       sentiment="neutral", anchor_text="x",
                       source_text="明天面试好紧张", confidence=0.85)
    s.add_commitment(c, contact_key="tg:u1", platform="telegram",
                     account_id="default", chat_key="u1")
    return s


class _AI:
    def __init__(self, reply="记得你说的面试，顺利吗？"):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _sender(rec):
    async def _send(*a, **k):
        rec.append(a)
        return 11
    return _send


# ── 纯函数 ──────────────────────────────────────────────────────────────────
def test_compose_extra_blocks_empty_is_empty():
    assert _compose_extra_blocks() == ""
    assert _compose_extra_blocks(persona_line=" ", memory_block="",
                                 goal_block=None or "") == ""


def test_compose_extra_blocks_sections():
    out = _compose_extra_blocks(persona_line="温柔絮叨，爱用波浪号",
                                memory_block="- 养了只猫叫团团",
                                goal_block="【工作目标背景】推进试用。")
    assert "你的说话风格" in out and "波浪号" in out
    assert "你记得的关于对方的事" in out and "团团" in out
    assert "【工作目标背景】" in out


def test_compose_extra_blocks_memory_uses_second_person():
    """#341：记忆事实「客户的妈妈在装修」注入前转「TA」，并附称谓约定——
    否则英文人设直译成 your client's mom（与 proactive_prompt 同口径）。"""
    out = _compose_extra_blocks(
        memory_block="- 客户的妈妈在装修\n- the customer's dog is sick")
    assert "TA的妈妈在装修" in out and "TA's dog is sick" in out
    assert "客户的妈妈" not in out and "customer's" not in out
    assert "your client" in out  # PERSPECTIVE_NOTE 里的禁用称谓提示
    assert "TA」「对方」都指你此刻正在聊的这个人本人" in out
    # 记忆块为空时不追加称谓约定（零 token 开销纪律不变）
    assert _compose_extra_blocks(persona_line="沉稳").count("称谓约定") == 0


def test_build_prompt_with_extras_and_without_is_backward_compatible():
    item = {"topic": "面试", "event_at": NOW, "source_text": "明天面试",
            "topic_norm": "面试"}
    base = build_care_prompt(item, context_block="ctx", now=NOW)
    enriched = build_care_prompt(
        item, context_block="ctx", now=NOW,
        persona_line="沉稳大方", memory_block="- 喜欢喝美式",
        goal_block="【工作目标背景】推进复购。")
    assert "沉稳大方" in enriched and "美式" in enriched and "复购" in enriched
    # 无增强时与旧口径等价（不含增强节标题）
    assert "你的说话风格" not in base
    assert "你记得的关于对方的事" not in base


# ── 派发器透传 ───────────────────────────────────────────────────────────────
async def test_dispatcher_passes_extras_into_prompt():
    s = _store_with()
    ai = _AI()
    calls = []

    def _extras(item):
        calls.append(dict(item))
        return {"persona_line": "俏皮活泼", "memory_block": "- 下月去东京",
                "goal_block": "【工作目标背景】推进订阅。"}

    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender([]),
                       context_provider=lambda ck: "ctx",
                       prompt_extras_provider=_extras)
    assert await d.run_once(now=NOW) == 1
    p = ai.prompts[0]
    assert "俏皮活泼" in p and "东京" in p and "推进订阅" in p
    assert calls and calls[0].get("topic") == "面试"  # provider 拿到整行


async def test_crisis_prompt_does_not_consume_extras():
    from src.contacts.care_schedule import CRISIS_CARE_TOPIC
    s = CareScheduleStore(":memory:")
    s.add_commitment(
        CareCommitment(due_at=NOW - 60, event_at=NOW - 60,
                       topic=CRISIS_CARE_TOPIC, sentiment="negative",
                       anchor_text="", source_text="", confidence=1.0),
        contact_key="tg:u2", platform="telegram", account_id="default",
        chat_key="u2", min_confidence=0.0, dedup_window_days=0.0)
    called = []
    ai = _AI(reply="我在，不用急着回我。")
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender([]),
                       context_provider=lambda ck: "ctx",
                       prompt_extras_provider=lambda it: called.append(1) or {
                           "persona_line": "俏皮"})
    assert await d.run_once(now=NOW) == 1
    assert called == []                      # 危机专线不取增强
    assert "俏皮" not in ai.prompts[0]        # 也绝不进克制模板


async def test_extras_provider_exception_does_not_block():
    s = _store_with()
    ai = _AI()

    def _boom(item):
        raise RuntimeError("provider down")

    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender([]),
                       context_provider=lambda ck: "ctx",
                       prompt_extras_provider=_boom)
    assert await d.run_once(now=NOW) == 1    # 照发（增强 fail-open）
    assert "面试" in ai.prompts[0]


def test_prompt_extras_public_method_for_preview_parity():
    s = CareScheduleStore(":memory:")
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender([]),
                       prompt_extras_provider=lambda it: {"persona_line": "p"})
    assert d.prompt_extras({"topic": "x"}) == {"persona_line": "p"}
    d2 = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender([]))
    assert d2.prompt_extras({"topic": "x"}) == {}


# ── 接线静态 ratchet（接线断了没有任何运行时报错，只能静态钉）────────────────
def test_background_tasks_wiring_pinned():
    src = (Path(__file__).resolve().parents[1]
           / "src" / "bootstrap" / "background_tasks.py").read_text(
        encoding="utf-8")
    for needle in (
        "prompt_extras_provider=_care_prompt_extras",
        "user_clock_provider=_care_user_clock",
        "already_discussed=_care_already_discussed",
        "sent_hook=_react_sent_hook",
        "CareGoalScanner(",
    ):
        assert needle in src, f"background_tasks 接线缺失: {needle}"
