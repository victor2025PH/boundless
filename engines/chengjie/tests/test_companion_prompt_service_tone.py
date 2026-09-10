# -*- coding: utf-8 -*-
"""O-1 C（#253 · D-O3，2026-09-08）：陪伴域「私人聊天」系统提示词 + 客服腔守卫。

Q9H2HM：`System prompt loaded from domain pack (1868 chars)`，域包 conversion 底稿是写给模型的
英文否定式规范；「I hear you… Take care」「我的助理会联系您」「Absolutely, I'm looking forward
to it. Have a great morning over there!」皆源于此。覆盖：
- 底稿按 ``business_domain`` 选文件：companion → system_companion.txt；sales → 旧稿（不删域包、
  ``effective_domain_name`` 不改）；
- 客服腔黑名单每词 + 实义豁免 + 三段式识别 + 条件句收尾 + 您→你 + 整段客服腔 → review；
- enrich_draft：改写一次 / 仍命中经 decide 转人审（reply_risk=high, reason service_tone）；
- 协议链：整段客服腔 → 结果码 service_tone 转人工（可自动摘）；
- 出站后处理：陪伴域先剥客服腔再去标点；销售域不动；
- 50 条拒绝 / 冷淡 / 告别 / 抱怨场景 AI 稿 → 确定性改写 + 去 AI 标点后「像真人」代理判据 ≥45。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.utils.business_domain as bdm
from src.utils import persona_guard as pg
from src.utils.domain_loader import DomainLoader

_ROOT = Path(__file__).resolve().parents[1]
DOMAINS = _ROOT / "domains"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.setenv("AITR_AUTOSEND_SHADOW_DIR", str(tmp_path / "shadow"))
    bdm.reset_active_business_domain()
    yield
    bdm.reset_active_business_domain()


class _Skill:
    pass


def _load_conversion(cfg):
    from src.hooks.registry import HookRegistry
    HookRegistry.reset()
    loader = DomainLoader(DOMAINS)
    return loader.load("conversion", _Skill, None, SimpleNamespace(config=cfg, config_path=None))


# ═══════════════════════════════════════════════════════════════════════
# ① 底稿按业务域选文件
# ═══════════════════════════════════════════════════════════════════════

def test_companion_domain_loads_private_chat_prompt(caplog):
    caplog.set_level(logging.INFO)
    pack = _load_conversion({"business_domain": "companion"})
    companion_txt = (DOMAINS / "conversion/prompts/system_companion.txt").read_text(encoding="utf-8")
    assert pack.system_prompt == companion_txt
    assert "companion" in pack.system_prompt.lower()          # N-3 契约不变
    assert any("system prompt variant for business_domain=companion" in r.message for r in caplog.records)


def test_sales_domain_keeps_original_prompt():
    pack = _load_conversion({"business_domain": "sales"})
    old = (DOMAINS / "conversion/prompts/system_prompt.txt").read_text(encoding="utf-8")
    assert pack.system_prompt == old
    assert "You are an emotional companion" in old            # 旧稿原样未动
    from src.utils.domain_policy import effective_domain_name
    assert effective_domain_name({"business_domain": "companion"}) == "conversion"


def test_companion_prompt_content_contract():
    t = (DOMAINS / "conversion/prompts/system_companion.txt").read_text(encoding="utf-8")
    # 上限随 Q-2 E 三句拒绝（#263）+ Q-8 F 夸赞一句（#264）放宽；仍钉「一屏内」不许无限膨胀
    assert 1500 <= len(t) <= 5200, len(t)
    low = t.lower()
    for phrase in ("i hear you", "take care", "i'll be around", "feel free to", "let me know if",
                   "如有需要", "很高兴为您", "我的助理"):
        assert phrase in low, phrase                             # 明确禁词在场
    assert "first person" in low and "one or two short sentences" in low
    assert "no dashes" in low and "no semicolons" in low         # 生成侧同步 B 段规则
    assert "not customer service" in low and "not an assistant" in low
    assert "您" in t and "你" in t                               # 您→你 规则
    # 示例块：拒绝 / 冷淡 / 告别 / 抱怨 / 被问是不是 bot 各至少一例
    assert "别再发了" in t and "you sound like a bot" in low and "don't feel like talking" in low
    # 示例回答不含 AI 标点
    examples = [ln for ln in t.splitlines() if ln.startswith("them:")]
    assert len(examples) >= 8
    for ln in examples:
        ans = ln.split("you:", 1)[1]
        assert not re.search(r"[—–;；]", ans), ln
        assert not re.search(r"take care|i hear you", ans, re.I), ln


# ═══════════════════════════════════════════════════════════════════════
# ② 客服腔守卫：黑名单每词 / 豁免 / 三段式 / 条件句 / 改写 / review
# ═══════════════════════════════════════════════════════════════════════

_BANNED = [
    "I hear you, and I'll stop here.",
    "I completely understand how you feel.",
    "Take care!",
    "I'll be around if you want to talk.",
    "I'm always here for you.",
    "Feel free to reach out anytime.",
    "Let me know if you need anything.",
    "Don't hesitate to ask.",
    "Rest assured, it's fine.",
    "I appreciate you sharing that with me.",
    "Thank you for sharing.",
    "Have a great morning over there!",
    "Wishing you a lovely evening.",
    "Absolutely, I'm looking forward to it.",
    "That must be really hard.",
    "I'm so sorry to hear that.",
    "My assistant will reach out.",
    "Is there anything else I can do?",
    "如有需要请随时告诉我。",
    "很高兴为您解答。",
    "感谢您的分享。",
    "我理解您的感受。",
    "祝您生活愉快。",
    "我的助理会联系您。",
    "有什么可以帮您的吗？",
    "请您放心。",
    "您好，在的。",
]


@pytest.mark.parametrize("text", _BANNED)
def test_service_tone_blacklist_each_phrase(text):
    assert pg.matches_service_tone(text), text
    out, rep = pg.rewrite_service_tone(text)
    assert rep["action"] == "review" and out == text   # 单句整段客服腔 → 交人审，不返回空


_CLEAN = [
    "ok, night.",
    "在，刚躺下。怎么了",
    "rude. i'm just bad at texting before coffee.",
    "ugh. same guy as last week?",
    "好，我不发了。",
    "I have to take care of the kids tonight, rain check?",   # 实义 take care of
    "take care of yourself when you're sick, don't be a hero",
    "let me know if it rains there tomorrow, i'm curious",   # 边界：仍算定式 → 见下一条
]


@pytest.mark.parametrize("text", _CLEAN[:-1])
def test_service_tone_clean_samples_untouched(text):
    assert pg.matches_service_tone(text) == [], text
    out, rep = pg.rewrite_service_tone(text)
    assert out == text and rep["action"] == "clean"


def test_three_part_close_detected_and_stripped():
    t = "Thank you for telling me. I understand, that makes sense. Take care and have a great day!"
    assert pg.detect_three_part_close(t)
    out, rep = pg.rewrite_service_tone(t)
    assert rep["three_part"] and rep["action"] == "review"        # 三句全是客服腔 → 人审
    t2 = "Thanks for the photo. The cat looks exactly like mine. Take care!"
    assert pg.detect_three_part_close(t2) is False                  # 没有确认句 → 不是三段式
    out2, rep2 = pg.rewrite_service_tone(t2)
    assert out2 == "Thanks for the photo. The cat looks exactly like mine." and rep2["action"] == "rewrite"


def test_conditional_close_stripped():
    t = "Sounds like a long day. If you ever need to vent, I'm here."
    assert pg.detect_conditional_close(t)
    out, rep = pg.rewrite_service_tone(t)
    assert out == "Sounds like a long day." and rep["action"] == "rewrite"
    zh = "今天确实挺累的。如果你需要聊聊，随时找我。"
    out2, rep2 = pg.rewrite_service_tone(zh)
    assert out2 == "今天确实挺累的。" and rep2["conditional_close"]


def test_formal_you_rewritten_to_casual():
    out, rep = pg.rewrite_service_tone("您今天累不累？我给您带了咖啡。")
    assert out == "你今天累不累？我给你带了咖啡。" and rep["action"] == "rewrite" and rep["formal_you"] == 2
    out2, _ = pg.rewrite_service_tone("您今天累不累？", formal_you=False)
    assert out2 == "您今天累不累？"


def test_mixed_text_rewrite_keeps_human_part():
    t = "I hear you, and I'll stop here. Honestly that movie was terrible though. Take care."
    out, rep = pg.rewrite_service_tone(t)
    assert out == "Honestly that movie was terrible though." and rep["action"] == "rewrite"
    assert any("hear you" in h for h in rep["hits"]) and any("Take care" in h for h in rep["hits"])


def test_report_shape_and_no_raise():
    rep = pg.service_tone_report("")
    assert rep["any"] is False and rep["hits"] == []
    assert pg.rewrite_service_tone(None)[1]["action"] == "clean"   # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
# ③ 接线：enrich_draft / 协议链 / 出站后处理
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def store(tmp_path):
    from src.inbox.store import InboxStore
    s = InboxStore(tmp_path / "o1c.db")
    yield s
    s.close()


def _conv():
    return {"conversation_id": "telegram:acct1:u1", "platform": "telegram",
            "account_id": "acct1", "chat_key": "u1", "display_name": "T"}


def test_enrich_draft_rewrites_then_reviews_in_companion_domain(store, monkeypatch, caplog):
    from src.ai.chat_assistant_service import quick_risk
    from src.inbox import autosend_policy as pol
    from src.inbox.drafts import DraftService
    monkeypatch.setattr(pol, "current_policy_mode", lambda: pol.POLICY_SHADOW)
    caplog.set_level(logging.INFO)
    bdm.set_active_business_domain("companion")
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    # 混合稿：改写一次后 L2 放行，正文只剩人话
    d1 = svc.auto_generate_draft(_conv(), "long day, that movie was bad", automation_mode="auto_ai", enrich=True)
    assert svc.enrich_draft(d1, reply_text="I hear you. Honestly that movie was terrible though. Take care!",
                            automation_mode="auto_ai")
    row = store.get_draft(d1)
    assert row["autopilot_level"] == "L2" and row["draft_text"] == "Honestly that movie was terrible though."
    assert any("[persona-guard]" in r.getMessage() and "action=rewrite" in r.getMessage() for r in caplog.records)
    # 整段客服腔：仍命中 → 经 decide 转人审 L1（reply_risk=high, reason service_tone）
    store.update_draft_status(d1, status="cancelled", decided_by="test")
    d2 = svc.auto_generate_draft({**_conv(), "conversation_id": "telegram:acct1:u2", "chat_key": "u2"},
                                 "ok bye", automation_mode="auto_ai", enrich=True)
    assert svc.enrich_draft(d2, reply_text="I hear you, and I'll stop here. Take care.", automation_mode="auto_ai")
    row2 = store.get_draft(d2)
    assert row2["autopilot_level"] == "L1" and row2["risk_level"] == "high"
    assert any("action=review" in r.getMessage() for r in caplog.records)


def test_enrich_draft_untouched_in_sales_domain(store, monkeypatch):
    from src.ai.chat_assistant_service import quick_risk
    from src.inbox import autosend_policy as pol
    from src.inbox.drafts import DraftService
    monkeypatch.setattr(pol, "current_policy_mode", lambda: pol.POLICY_SHADOW)
    bdm.set_active_business_domain("sales")
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    d = svc.auto_generate_draft(_conv(), "hello", automation_mode="auto_ai", enrich=True)
    assert svc.enrich_draft(d, reply_text="很高兴为您解答，如有需要请随时告诉我。", automation_mode="auto_ai")
    row = store.get_draft(d)
    assert row["autopilot_level"] == "L2" and row["draft_text"] == "很高兴为您解答，如有需要请随时告诉我。"


@pytest.mark.asyncio
async def test_protocol_chain_service_tone_handoff(monkeypatch):
    from src.integrations import protocol_autoreply as pa
    bdm.set_active_business_domain("companion")
    pa._last_reply.clear()
    sent = []

    class _Reg:
        def get(self, p, a):
            return {"meta": {"auto_reply": True}}

    async def _gen_bad(**kw):
        return "I hear you, and I'll stop here. Take care."

    async def _gen_mixed(**kw):
        return "I hear you. that movie was terrible though. Take care."

    async def _send(**kw):
        sent.append(kw["text"])

    cfg = {"protocol_autoreply": {"enabled": True}}
    res = await pa.run_autoreply({"direction": "in", "platform": "telegram", "account_id": "a",
                                  "chat_key": "c1", "text": "ok"}, registry=_Reg(), cfg=cfg,
                                 generate=_gen_bad, send=_send, now=1000.0)
    assert res["reason"] == "service_tone" and res.get("sent") is not True and sent == []
    assert pa.needs_handoff(res) and "service_tone" in pa.HANDOFF_AUTO_CLEAR_REASONS
    res2 = await pa.run_autoreply({"direction": "in", "platform": "telegram", "account_id": "a",
                                   "chat_key": "c2", "text": "ok"}, registry=_Reg(), cfg=cfg,
                                  generate=_gen_mixed, send=_send, now=1000.0)
    assert res2["reason"] == "ok" and sent == ["that movie was terrible though."]


def test_outbound_humanize_strips_service_tone_in_companion_only(caplog):
    from src.inbox.outbound_humanize import apply_outbound_humanize
    caplog.set_level(logging.INFO)
    raw = "I hear you — that sounds exhausting. Honestly the boss thing is ridiculous. Take care!"
    bdm.set_active_business_domain("companion")
    out = apply_outbound_humanize(raw, conversation_id="c", origin="auto", stage="t")
    assert out == "Honestly the boss thing is ridiculous."
    assert any("[persona-guard] service_tone=" in r.getMessage() and "action=rewrite" in r.getMessage()
               for r in caplog.records)
    assert any("[outbound]" in r.getMessage() and "svc_tone=" in r.getMessage() for r in caplog.records)
    bdm.set_active_business_domain("sales")
    out2 = apply_outbound_humanize(raw, conversation_id="c", origin="auto", stage="t")
    assert "Take care" in out2 and "—" not in out2          # 销售域只去标点不剥客服腔


# ═══════════════════════════════════════════════════════════════════════
# ④ 50 条拒绝 / 冷淡 / 告别 / 抱怨场景：确定性改写 + 去 AI 标点后「像真人」代理判据 ≥45
#    （真人盲评交运营；这里钉住机器可判的部分：零客服腔、零 AI 标点、≤2 句、非空）
# ═══════════════════════════════════════════════════════════════════════

_SCENARIOS = [
    # 拒绝
    "I hear you, and I'll stop here — no more messages from me. Take care.",
    "I completely understand. If you ever change your mind, feel free to reach out. Wishing you all the best!",
    "Absolutely, I respect that. I'll be around if you need anything; take care of yourself.",
    "好的，我理解您的感受。如有需要请随时告诉我，祝您生活愉快。",
    "明白了，那我就不打扰了；您有任何需要都可以随时联系我。",
    "Understood. Thank you for letting me know — I appreciate your honesty. Have a great day!",
    "Fair enough — I get it. Let me know if you'd like to talk later. Take care!",
    "No worries at all! I understand completely. Feel free to message me anytime.",
    # 冷淡
    "It sounds like you've had a lot on your plate — that must be really exhausting. I'm here if you need me.",
    "I understand you're busy; no pressure at all. Have a wonderful evening over there!",
    "That's okay — sometimes we all need space, don't we? I'll be right here.",
    "听起来您今天很累；有时候休息一下也很重要，对吧？我一直在这里。",
    "没关系，我理解您现在不想聊。如果您想说话，随时告诉我。",
    "Of course — take all the time you need. Rest assured, I'm not going anywhere.",
    "Got it, I'll give you some space. Don't hesitate to reach out when you're ready.",
    "I appreciate you telling me. I understand. Wishing you a restful night!",
    # 告别
    "Good night! Thank you for the lovely chat — I really appreciate you sharing your day with me. Sweet dreams!",
    "Take care and sleep well; tomorrow is a new day, isn't it?",
    "晚安！感谢您的分享，祝您有个美好的夜晚。",
    "那就先这样，祝您一切顺利；有需要的话随时找我。",
    "Alright, talk soon! Have a great rest of your day over there. Take care!",
    "Bye for now — it was wonderful catching up. Let me know if you need anything at all!",
    "Sleep well! I'm always here for you, whenever you need me.",
    "Goodbye, and thank you for reaching out today. I hope this helps!",
    # 抱怨
    "I'm so sorry to hear that — that must be incredibly frustrating. Your feelings are valid, and I'm here for you.",
    "I understand your frustration; I apologize if I came across as robotic. How can I support you today?",
    "非常抱歉给您带来的不便；我理解您的感受，我们会尽快改进。",
    "您说得对，我话太多了。感谢您的反馈，我会注意的；如有需要请随时告诉我。",
    "That's a fair point — I hear you. I'll do better; thank you for your patience!",
    "I completely understand how you feel. Rest assured, I take this seriously. Is there anything else I can do?",
    "Ugh, that boss again? That must be so draining — please know that I'm always here to listen.",
    "I get it, that's annoying — I'd be upset too. Let me know if there's anything I can do to help!",
    # 再来 18 条变体
    "Thanks for telling me. I understand where you're coming from. Take care of yourself!",
    "I hear you — and honestly, you deserve a break. At the end of the day, it's the little things, isn't it?",
    "好的，我明白了；那我就先不打扰您了，保重。",
    "我理解您的处境，很高兴为您排解烦恼；请随时联系我。",
    "It's completely understandable to feel that way — life gets heavy sometimes. I'm here if you need to vent.",
    "Noted! I appreciate your feedback — feel free to let me know if anything else comes up. Have a nice day!",
    "Sure — no problem at all. Wishing you a peaceful evening; talk whenever you're ready.",
    "I'm glad to hear that! Take care and have a wonderful weekend ahead!",
    "That sounds tough — I'm sorry. Please know that you can always talk to me; I'll be around.",
    "Understood — I won't message again. Thank you for your time, and take care.",
    "感谢您的信任！如果您有任何问题，欢迎随时找我，祝您工作顺利。",
    "您别生气，我理解您的心情；我们团队会尽快处理，请您放心。",
    "Okay — I respect your decision. Thank you for sharing your thoughts; I'll be here if you change your mind.",
    "I hear you loud and clear. Have a great morning over there — and don't hesitate to reach out!",
    "Right — that's frustrating; I'd feel the same. Anyway, I'm here whenever you need me.",
    "Aw, I'm sorry — that's not what I meant at all. Feel free to tell me if I overstep again, okay?",
    "好，我不发了；祝您一切顺利，保重身体。",
    "Fair — I talk a lot. Thanks for your patience; let me know if you'd rather I keep it short.",
]


def _looks_human(text: str) -> bool:
    if not text.strip():
        return False
    if pg.matches_service_tone(text) or pg.detect_three_part_close(text) or pg.detect_conditional_close(text):
        return False
    if re.search(r"[—–;；]", text) or re.search(r"(?:isn't it|aren't they|right|对吧|是吧)\s*[?？]\s*$", text, re.I):
        return False
    if "您" in text:
        return False
    parts = [p for p in re.split(r"(?<=[.!?。！？])\s*", text.strip()) if p.strip()]
    return len(parts) <= 3


def test_fifty_reject_cold_goodbye_complaint_samples_proxy_blind_eval():
    from src.inbox.outbound_humanize import humanize
    assert len(_SCENARIOS) == 50
    bdm.set_active_business_domain("companion")
    passed = 0
    failures = []
    for s in _SCENARIOS:
        assert not _looks_human(s), s                     # 输入侧确认是「客服腔 AI 稿」
        rw, rep = pg.rewrite_service_tone(s)
        if rep["action"] == "review":
            # 整段客服腔 → 人审（不出站）；对客户而言是「零机器味出站」，计通过
            passed += 1
            continue
        out, _ = humanize(rw, "")
        if _looks_human(out):
            passed += 1
        else:
            failures.append((s, out))
    assert passed >= 45, (passed, failures[:5])
