# -*- coding: utf-8 -*-
"""通话运行时装配门禁：build_call_runtime 组装、on_incoming 决策路由、dry_run_report 完整性自检。

全用 fake transport/brain/lookups（无 pytgcalls / 无 live host / 无 GPU）。
"""
import asyncio

from src.voicecall.core import CallsConfig
from src.voicecall.runtime import CallRuntimeDeps, build_call_runtime


class FakeTransport:
    def __init__(self):
        self.answered, self.declined, self.frames, self.hungup = [], [], [], []
    async def answer(self, c): self.answered.append(c)
    async def decline(self, c): self.declined.append(c)
    async def send_frame(self, c, f): self.frames.append(f)
    async def hangup(self, c): self.hungup.append(c)


class FakeBrainSession:
    def __init__(self, evs): self._evs = evs; self.pushed = []; self.directives = []; self.closed = False
    async def push_audio(self, p): self.pushed.append(p)
    async def inject_directive(self, t): self.directives.append(t)
    async def events(self):
        for e in self._evs:
            yield e
    async def close(self): self.closed = True


class FakeBrain:
    def __init__(self, evs=()): self._evs = list(evs); self.opened = []
    async def open(self, ctx): self.opened.append(ctx); return FakeBrainSession(self._evs)


def _cfg(**tc):
    base = {"enabled": True, "transport_verified": True,
            "answer": {"min_intimacy": 30, "languages": ["zh", "en"]},
            "budget": {"daily_calls_cap": 20}}
    base.update(tc)
    return {"telegram_calls": base, "realtime_voice": {"base_url": "http://h:7860"}}


def _known_contact(a, c):
    return {"has_conversation": True, "peer_known": True, "language": "zh",
            "automation_mode": "auto_ai", "intimacy": 70}


# ── on_incoming 决策路由 ─────────────────────────────────────────────────────
def test_on_incoming_accept_starts_session():
    tp, br = FakeTransport(), FakeBrain([{"type": "session.end"}])
    deps = CallRuntimeDeps(conversation_lookup=_known_contact,
                           usage_lookup=lambda k: (1, 5.0),
                           memory_lookup=lambda k: "喜欢猫")
    rt = build_call_runtime(_cfg(), transport=tp, brain=br, deps=deps)

    async def _go():
        action = await rt.on_incoming(555, "acc1")
        # 等后台 run_session 跑完
        for _ in range(20):
            if not rt._tasks:
                break
            await asyncio.sleep(0.02)
        return action
    action = asyncio.run(_go())
    assert action == "accept"
    assert tp.answered == [555]
    assert br.opened and br.opened[0].memory_bullets == "喜欢猫"


def test_on_incoming_stranger_silent_decline():
    tp, br = FakeTransport(), FakeBrain()
    deps = CallRuntimeDeps(conversation_lookup=lambda a, c: None)  # 查无会话=陌生人
    rt = build_call_runtime(_cfg(), transport=tp, brain=br, deps=deps)
    action = asyncio.run(rt.on_incoming(999, "acc1"))
    assert action == "decline_silent"
    assert tp.declined == [999] and tp.answered == []


def test_on_incoming_budget_red_compensates():
    tp, br = FakeTransport(), FakeBrain()
    comp = []

    async def _comp(ctx, reason): comp.append(reason)
    deps = CallRuntimeDeps(conversation_lookup=_known_contact,
                           account_light_lookup=lambda k: "red",
                           compensate=_comp)
    rt = build_call_runtime(_cfg(), transport=tp, brain=br, deps=deps)
    action = asyncio.run(rt.on_incoming(555, "acc1"))
    assert action == "decline_compensate"
    assert comp == ["account_unhealthy"]


# ── wrapup 闭环：用量记账 + 记忆落库串通 ─────────────────────────────────────
def test_runtime_wrapup_records_usage_and_memory():
    recorded, facts = [], []
    tp = FakeTransport()
    br = FakeBrain([{"type": "transcript.user", "text": "我在学吉他"},
                   {"type": "session.end"}])
    deps = CallRuntimeDeps(
        conversation_lookup=_known_contact,
        usage_record=lambda ak, dur: recorded.append((ak, dur)),
        memory_add=lambda k, f: facts.append((k, f)))
    # extract 走默认启发式；这里重点验证 usage 记账（预算环闭合）必发生
    rt = build_call_runtime(_cfg(), transport=tp, brain=br, deps=deps)

    async def _go():
        await rt.on_incoming(555, "acc1")
        for _ in range(30):
            if not rt._tasks:
                break
            await asyncio.sleep(0.02)
    asyncio.run(_go())
    assert len(recorded) == 1
    assert recorded[0][0] == "telegram:acc1"       # 账号键
    assert recorded[0][1] >= 0.0                     # 时长


# ── dry_run_report 完整性自检 ────────────────────────────────────────────────
def test_dry_run_report_flags_missing_deps():
    tp, br = FakeTransport(), FakeBrain()
    # 只接了 transport/brain，其余关键依赖全缺
    rt = build_call_runtime(_cfg(), transport=tp, brain=br,
                            deps=CallRuntimeDeps())
    rep = rt.dry_run_report(host_probe={"reachable": True, "model_loaded": True})
    assert rep["enabled"] is True
    assert rep["wired"]["transport"] is True
    assert rep["wired"]["conversation_lookup"] is False
    # 关键缺失项被点名（环没闭）
    assert any("conversation_lookup" in m for m in rep["missing"])
    assert any("usage" in m for m in rep["missing"])
    assert any("memory_lookup" in m for m in rep["missing"])


def test_dry_run_report_all_wired_no_missing():
    tp, br = FakeTransport(), FakeBrain()
    deps = CallRuntimeDeps(
        conversation_lookup=_known_contact, usage_lookup=lambda k: (0, 0.0),
        usage_record=lambda ak, d: None, memory_lookup=lambda k: "",
        memory_add=lambda k, f: None)
    rt = build_call_runtime(_cfg(), transport=tp, brain=br, deps=deps)
    rep = rt.dry_run_report(host_probe={"reachable": True, "model_loaded": True})
    assert rep["missing"] == []
    assert rep["readiness"]["ready"] is True         # 主机在线 + transport_verified + 有会话


def test_dry_run_report_disabled():
    tp, br = FakeTransport(), FakeBrain()
    rt = build_call_runtime({"telegram_calls": {"enabled": False}},
                            transport=tp, brain=br, deps=CallRuntimeDeps())
    rep = rt.dry_run_report()
    assert rep["enabled"] is False
    assert rep["missing"] == []                      # 未启用不苛求接线
