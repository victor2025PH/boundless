# -*- coding: utf-8 -*-
"""B 线（generate_inbox_draft）× kb_policy 接线门禁（P0-2 ～ P0-5，2026-09-29）。

复现 8E56 机实况：客服人设 q567 + 客户问「那都有什么活动」（direct_chat，无支付词）。
旧闸在检索前跳过知识库；现在：
  1. 客服人设 → 必查（fake KB 的 search 被调用）；
  2. 查无 → 第一次注入固定话术指令、第二次标需人工（risk_hold needs_human）+ 已转人工指令；
  3. 命中 → 权威事实措辞进 prompt、连续查无计数清零；
  4. 陪聊人设同句 → 旧闲聊闸原样跳过；
  5. 返回值带 kb_decision，决策登记表可按会话读回（草稿条 / smart-reply 用）。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.skills.skill_manager import SkillManager
from src.utils import kb_policy as kp
from src.utils.config_manager import ConfigManager

_Q567 = {"id": "support_k3a629", "name": "q567", "role": "售后支持专员",
         "tags": ["客服", "售后", "耐心"]}
_BESTIE = {"id": "lin", "name": "林小雨", "role": "温柔陪聊 / 治愈系闺蜜", "tags": ["温柔"]}
_CID = "telegram:8414394703:7380618071"


async def _make_cm(tmp_path: Path) -> ConfigManager:
    cfg = {
        "domain": "conversion",
        "business_domain": "companion",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
        "memory": {"enabled": False, "extract": {"enabled": False}},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text("channels: {}\n", encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    return cm


class _FakeKB:
    def __init__(self, entries=None):
        self.entries = list(entries or [])
        self.search_calls = []
        self.misses = []

    def search(self, text, top_k=3, lang="zh", **kw):
        self.search_calls.append(text)
        return {"entries": list(self.entries), "search_mode": "bm25"}

    def build_ai_context_from_result(self, res, lang="zh"):
        ents = res.get("entries") or []
        return "\n".join(f"▶ [{e['category']}] {e['title']}\n  【示例回复】: {e['example_reply_zh']}"
                         for e in ents)

    def log_miss(self, q):
        self.misses.append(q)

    def get_direct_reply(self, key):
        return None


class _FakeStore:
    def __init__(self):
        self.kv = {}

    def get_app_setting(self, k, default=""):
        return self.kv.get(k, default)

    def set_app_setting(self, k, v, updated_by=""):
        self.kv[k] = v


def _wire(monkeypatch, sm, kb, persona, store):
    monkeypatch.setattr(sm, "_kb_store_if_exists", lambda: kb)
    from src.utils.persona_manager import PersonaManager
    pm = PersonaManager.get_instance()
    monkeypatch.setattr(pm, "get_persona_with_tier",
                        lambda chat_id="", account_persona_id="", conversation_key="": (persona, "account_profile"))
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "get_inbox_store", lambda *a, **k: store)


def _ai(captured):
    ai = MagicMock()

    async def _gen(**kw):
        ctx = kw.get("user_context") or {}
        captured.append({
            "kb_context": ctx.get("kb_context"),
            "nohit": ctx.get("_kb_nohit_block"),
            "decision": ctx.get("_kb_decision"),
        })
        return "好的，我看一下。"

    ai.generate_reply_with_intent = AsyncMock(side_effect=_gen)
    return ai


@pytest.fixture(autouse=True)
def _clean():
    kp._reset_for_tests()
    yield
    kp._reset_for_tests()


@pytest.mark.asyncio
async def test_support_persona_queries_kb_and_escalates_on_second_nohit(tmp_path, monkeypatch):
    cm = await _make_cm(tmp_path)
    captured = []
    sm = SkillManager(cm, _ai(captured))
    kb, store = _FakeKB(entries=[]), _FakeStore()
    _wire(monkeypatch, sm, kb, _Q567, store)

    out = await sm.generate_inbox_draft(
        text="那都有什么活动", chat_key="7380618071", platform="telegram",
        history=[{"role": "user", "content": "那都有什么活动"}],
        persona_id="support_k3a629", conversation_id=_CID, account_id="8414394703",
    )
    assert out is not None
    # ① 旧闸会跳过；现在必查
    assert kb.search_calls == ["那都有什么活动"]
    d = out["kb_decision"]
    assert d["mode"] == "must" and d["kind"] == "support" and d["hit"] is False
    assert d["nohit_n"] == 1 and d["handoff"] is False
    # ② 第一次查无：固定话术指令进 prompt；客户原话进学习池（不受「像不像提问」守门）
    assert captured[-1]["nohit"] and "【知识库查无】" in captured[-1]["nohit"]
    assert kp.NOHIT_REPLY_ZH in captured[-1]["nohit"]
    assert "那都有什么活动" in kb.misses
    assert not captured[-1]["kb_context"]
    # 登记表可按会话读回（/api/drafts 富集口）
    assert kp.peek_decision(_CID)["nohit_n"] == 1
    # 此时不该标需人工
    from src.inbox import risk_hold
    assert risk_hold.active(store, _CID) is None

    # ③ 客户追问 → 第二次查无 → 标需人工 + 已转人工指令
    out2 = await sm.generate_inbox_draft(
        text="那到底有什么活动", chat_key="7380618071", platform="telegram",
        history=[{"role": "user", "content": "那到底有什么活动"}],
        persona_id="support_k3a629", conversation_id=_CID, account_id="8414394703",
    )
    d2 = out2["kb_decision"]
    assert d2["nohit_n"] == 2 and d2["handoff"] is True
    assert "已转人工" in captured[-1]["nohit"]
    assert risk_hold.active(store, _CID) == "needs_human"
    rec = json.loads(store.kv[[k for k in store.kv if k.endswith(_CID)][0]])
    assert rec["hit"] == "kb_nohit_repeat" and rec["by"] == "kb_policy"


@pytest.mark.asyncio
async def test_support_persona_hit_injects_authoritative_block_and_resets_counter(tmp_path, monkeypatch):
    cm = await _make_cm(tmp_path)
    captured = []
    sm = SkillManager(cm, _ai(captured))
    kb = _FakeKB(entries=[{"id": "e1", "category": "活动优惠", "title": "网站有什么活动",
                           "example_reply_zh": "新人注册送 88 体验金，VIP 等级另有返利。",
                           "reply_mode": "ai_guided"}])
    store = _FakeStore()
    _wire(monkeypatch, sm, kb, _Q567, store)
    kp.nohit_bump(_CID)   # 假装上一题查无过一次

    out = await sm.generate_inbox_draft(
        text="那都有什么活动", chat_key="7380618071", platform="telegram",
        history=[{"role": "user", "content": "那都有什么活动"}],
        persona_id="support_k3a629", conversation_id=_CID, account_id="8414394703",
    )
    d = out["kb_decision"]
    assert d["mode"] == "must" and d["hit"] is True and d["refs"] == 1
    assert out["kb_refs"] and out["kb_refs"][0]["title"] == "网站有什么活动"
    assert "新人注册送 88 体验金" in (captured[-1]["kb_context"] or "")
    assert not captured[-1]["nohit"]
    assert kp.nohit_count(_CID) == 0          # 命中清零
    assert kb.misses == []


@pytest.mark.asyncio
async def test_support_persona_non_question_miss_does_not_trigger_fallback(tmp_path, monkeypatch):
    """P1-1：客服人设收到「谢谢」→ 仍查库（must），查无但不是提问 → 不注固定话术、不计连续查无、不入学习池。"""
    cm = await _make_cm(tmp_path)
    captured = []
    sm = SkillManager(cm, _ai(captured))
    kb, store = _FakeKB(entries=[]), _FakeStore()
    _wire(monkeypatch, sm, kb, _Q567, store)
    out = await sm.generate_inbox_draft(
        text="好的谢谢", chat_key="7380618071", platform="telegram",
        history=[{"role": "user", "content": "好的谢谢"}],
        persona_id="support_k3a629", conversation_id=_CID, account_id="8414394703",
    )
    assert kb.search_calls == ["好的谢谢"]
    d = out["kb_decision"]
    assert d["mode"] == "must" and d["hit"] is False and d["asked"] is False
    assert d["nohit_n"] == 0 and d["handoff"] is False
    assert not captured[-1]["nohit"]
    assert kb.misses == []
    assert kp.nohit_count(_CID) == 0


@pytest.mark.asyncio
async def test_companion_persona_keeps_chitchat_gate(tmp_path, monkeypatch):
    cm = await _make_cm(tmp_path)
    captured = []
    sm = SkillManager(cm, _ai(captured))
    kb, store = _FakeKB(entries=[]), _FakeStore()
    _wire(monkeypatch, sm, kb, _BESTIE, store)

    out = await sm.generate_inbox_draft(
        text="那都有什么活动", chat_key="u2", platform="telegram",
        history=[{"role": "user", "content": "那都有什么活动"}],
        persona_id="lin", conversation_id="telegram:acc:u2",
    )
    assert kb.search_calls == []               # 旧闸原样：闲聊不查
    d = out["kb_decision"]
    assert d["mode"] == "skip" and d["reason"] == "companion_chat" and d["kind"] == "companion"
    assert not captured[-1]["nohit"] and not captured[-1]["kb_context"]
    assert kp.peek_decision("telegram:acc:u2")["mode"] == "skip"


def test_ai_client_prompt_uses_kind_wording():
    """ai_client 注入段按 kind 分套（format_kb_block 单源）；查无块只在无 kb_context 时注入。"""
    import inspect
    from src.ai.ai_client import AIClient
    src = inspect.getsource(AIClient)
    assert "format_kb_block" in src and "_kb_nohit_block" in src
    # 旧无差别块头「【知识库参考（仅供话术风格参考）】」已移入 kb_policy 的 live_channel 分支
    assert "【知识库参考（仅供话术风格参考）】" not in src


def test_miss_chip_deep_links_to_knowledge_prefill():
    """P1-3：未命中 chip → /knowledge?new=1&q= 预填新建抽屉。"""
    root = Path(__file__).resolve().parents[1]
    inbox = (root / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    kb = (root / "src" / "web" / "templates" / "knowledge.html").read_text(encoding="utf-8")
    assert "function openKbMissAdd" in inbox
    assert "/knowledge?new=1&q=" in inbox
    assert "function openKbEmbedHealth" in inbox
    assert "/knowledge?health=embed" in inbox
    assert "kb2_prefill_scenario" in kb
    assert "_qs.get('new')" in kb
    assert "_qs.get('health')==='embed'" in kb
    assert 'id="embed-health-row"' in kb
