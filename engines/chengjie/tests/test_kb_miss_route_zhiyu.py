"""智语 2026-10-08：KB 新命中口径接入回复兜底；未命中进 miss_log + 生成 kb_drafts 待审。"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.utils import kb_miss_route as kmr
from src.utils import kb_policy as kp
from src.utils.kb_store import KnowledgeBaseStore

ENGINE = Path(__file__).resolve().parents[1]
Q = "你们周末营业吗？"


@pytest.fixture(autouse=True)
def _clean():
    kmr._reset_for_tests()
    kp._reset_for_tests()
    yield
    kmr._reset_for_tests()
    kp._reset_for_tests()


def _kb(tmp_path):
    return KnowledgeBaseStore(tmp_path / "knowledge_base.db")


def _learner(kb, ai=True):
    from src.utils.daily_learner import DailyLearner
    ln = DailyLearner(kb, ai_client=MagicMock() if ai else None)
    calls = []

    async def fake_generate(materials, domain_context=""):
        calls.append([m["query"] for m in materials])
        return [{"source": m["source"], "query": m["query"], "hit_count": m["count"],
                 "category": "其他", "title": "营业时间", "triggers": ["营业", "周末"],
                 "example_reply": "周末照常营业，时间以店铺公告为准。",
                 "source_ref": m.get("source_ref", "")} for m in materials]

    ln.generate_drafts = fake_generate
    return ln, calls


def _drafts(ln):
    with ln._conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM kb_drafts").fetchall()]


def _misses(kb):
    return {r["query"]: r["cnt"] for r in kb.get_miss_stats(top_k=50)}


# ── 命中判定（回复链与统计同口径）───────────────────────────────────────────

def test_hit_for_reply_uses_configured_vector_threshold():
    res = {"entries": [{"id": "e1", "title": "退款政策", "category": "售后", "_vec_sim": 0.7}]}
    assert kmr.kb_hit_for_reply("can I get my money back", res, {})[0] is True
    strict = {"knowledge_base": {"hit_min_vec_sim": 0.9}}
    hit, why = kmr.kb_hit_for_reply("can I get my money back", res, strict)
    assert hit is False and why.startswith("weak")
    assert kmr.kb_hit_for_reply(Q, {"entries": []}, {}) == (False, "no_entries")


def test_hit_for_reply_never_raises():
    assert kmr.kb_hit_for_reply(Q, {"entries": [object()]}, None)[0] is False


# ── 未命中：入池 + 待审草稿 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_question_miss_logged_and_draft_generated(tmp_path):
    kb = _kb(tmp_path)
    ln, calls = _learner(kb)
    out = kmr.record_kb_miss(kb, Q, conversation_id="whatsapp:acc:peer", lang="zh", reason="no_entries")
    assert out["logged"] is True and out["draft"] == "scheduled"
    await kmr._drain_for_tests()
    assert calls == [[Q]]
    rows = _drafts(ln)
    assert len(rows) == 1
    d = rows[0]
    assert d["status"] == "pending" and d["source"] == kmr.LIVE_DRAFT_SOURCE
    assert d["query"] == Q and d["source_ref"] == "conv:whatsapp:acc:peer"
    assert _misses(kb).get(Q) == 1          # miss 记录保留（未命中统计 / chip 照常）


@pytest.mark.asyncio
async def test_same_question_again_does_not_call_llm_twice(tmp_path):
    kb = _kb(tmp_path)
    ln, calls = _learner(kb)
    kmr.record_kb_miss(kb, Q, conversation_id="c1")
    await kmr._drain_for_tests()
    kmr.record_kb_miss(kb, "你们周末营业吗", conversation_id="c2")   # 同题不同标点
    await kmr._drain_for_tests()
    assert len(calls) == 1 and len(_drafts(ln)) == 1
    assert sum(_misses(kb).values()) == 2


@pytest.mark.asyncio
async def test_ai_unavailable_keeps_material_in_pool(tmp_path):
    kb = _kb(tmp_path)
    ln, calls = _learner(kb, ai=False)
    out = kmr.record_kb_miss(kb, Q)
    assert out["logged"] is True and out["draft"] == "ai_unavailable"
    assert calls == [] and _drafts(ln) == []
    assert Q in _misses(kb)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["好的", "谢谢", "[图片]", "😂😂", "ok po"])
async def test_noise_is_neither_logged_nor_drafted(tmp_path, text):
    kb = _kb(tmp_path)
    ln, calls = _learner(kb)
    out = kmr.record_kb_miss(kb, text)
    assert out["logged"] is False and out["draft"] in {"not_a_question", "no_text"}
    await kmr._drain_for_tests()
    assert calls == [] and _misses(kb) == {}


@pytest.mark.asyncio
async def test_policy_already_logged_is_not_double_counted(tmp_path):
    kb = _kb(tmp_path)
    ln, calls = _learner(kb)
    kb.log_miss(Q)                           # _kb_after_search（must 档）已入池
    out = kmr.record_kb_miss(kb, Q, already_logged=True)
    assert out["logged"] == "by_policy" and out["draft"] == "scheduled"
    await kmr._drain_for_tests()
    assert _misses(kb)[Q] == 1 and len(_drafts(ln)) == 1


@pytest.mark.asyncio
async def test_rate_limit_and_switch_off(tmp_path):
    kb = _kb(tmp_path)
    ln, calls = _learner(kb)
    one = {"knowledge_base": {"live_miss_drafts_per_hour": 1}}
    assert kmr.record_kb_miss(kb, Q, config=one)["draft"] == "scheduled"
    assert kmr.record_kb_miss(kb, "退货运费谁出？", config=one)["draft"] == "rate_limited"
    off = {"knowledge_base": {"live_miss_drafts_per_hour": 0}}
    assert kmr.record_kb_miss(kb, "可以开发票吗？", config=off)["draft"] == "rate_limited"
    await kmr._drain_for_tests()
    assert len(calls) == 1
    assert {"退货运费谁出？", "可以开发票吗？"} <= set(_misses(kb))


def test_without_event_loop_only_logs(tmp_path):
    kb = _kb(tmp_path)
    ln, calls = _learner(kb)
    out = kmr.record_kb_miss(kb, Q)
    assert out["logged"] is True and out["draft"] == "no_loop"
    assert calls == []


def test_store_without_db_path_is_safe():
    class Bare:
        def __init__(self):
            self.misses = []

        def log_miss(self, q):
            self.misses.append(q)

    b = Bare()
    out = kmr.record_kb_miss(b, Q)
    assert out["logged"] is True and out["draft"] == "no_learner" and b.misses == [Q]
    assert kmr.record_kb_miss(None, Q)["draft"] == "no_text"


# ── skill_manager 接线 ─────────────────────────────────────────────────────

def _sm_src():
    return (ENGINE / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8")


def test_both_kb_paths_use_new_hit_for_fallback():
    src = _sm_src()
    calls = re.findall(r"self\._kb_after_search\(\s*user_context, _kb_decision, _kb, text, hit=(\w+)", src)
    assert calls == ["_hit_q", "_hit_q"], calls
    assert len(re.findall(r"self\._kb_route_miss\(", src)) == 2
    assert "kb_hit_for_reply(text, _res, self.config)" in src
    assert "_kb.log_miss(text)" not in src     # 入池只剩 record_kb_miss / _kb_after_search 两处


def test_weak_hit_cannot_go_direct():
    src = _sm_src()
    assert 'if _top_reply_mode == "direct" and not _hit_q:' in src


# ── B 线（收件箱草稿）端到端：弱命中按查无兜底 ─────────────────────────────

_SUPPORT = {"id": "support_zy", "name": "zy", "role": "售后支持专员", "tags": ["客服", "售后"]}
_CID = "telegram:acc1:peer1"


async def _make_cm(tmp_path):
    from src.utils.config_manager import ConfigManager
    cfg = {
        "domain": "conversion", "business_domain": "companion",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"}, "skills": {"enabled": []},
        "intent": {"keywords": {}, "patterns": {}}, "reply": {},
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
    def __init__(self, entries):
        self.entries = list(entries)
        self.misses = []

    def search(self, text, top_k=3, lang="zh", **kw):
        return {"entries": list(self.entries), "search_mode": "bm25"}

    def build_ai_context_from_result(self, res, lang="zh"):
        return "\n".join(f"▶ [{e['category']}] {e['title']}\n  【示例回复】: {e['example_reply_zh']}"
                         for e in res.get("entries") or [])

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
        captured.append({"kb_context": ctx.get("kb_context"), "nohit": ctx.get("_kb_nohit_block")})
        return "好的，我帮你确认一下。"

    ai.generate_reply_with_intent = AsyncMock(side_effect=_gen)
    return ai


@pytest.mark.asyncio
async def test_inbox_draft_weak_hit_falls_back_and_logs_once(tmp_path, monkeypatch):
    from src.skills.skill_manager import SkillManager
    cm = await _make_cm(tmp_path)
    captured = []
    sm = SkillManager(cm, _ai(captured))
    kb = _FakeKB([{"id": "e9", "category": "售后", "title": "退款政策",
                   "example_reply_zh": "七天无理由退款。", "reply_mode": "ai_guided"}])
    _wire(monkeypatch, sm, kb, _SUPPORT, _FakeStore())
    out = await sm.generate_inbox_draft(
        text=Q, chat_key="peer1", platform="telegram",
        history=[{"role": "user", "content": Q}],
        persona_id="support_zy", conversation_id=_CID, account_id="acc1",
    )
    d = out["kb_decision"]
    assert d["mode"] == "must" and d["hit"] is False and d["nohit_n"] == 1
    assert captured[-1]["nohit"] and "【知识库查无】" in captured[-1]["nohit"]
    assert not captured[-1]["kb_context"]          # 弱命中材料撤下，兜底话术才生效
    assert not out.get("kb_refs")
    assert kb.misses == [Q]                        # 入池一次（不双记）


@pytest.mark.asyncio
async def test_inbox_draft_real_hit_still_injects(tmp_path, monkeypatch):
    from src.skills.skill_manager import SkillManager
    cm = await _make_cm(tmp_path)
    captured = []
    sm = SkillManager(cm, _ai(captured))
    kb = _FakeKB([{"id": "e1", "category": "常规咨询", "title": "周末营业时间",
                   "example_reply_zh": "周末 10 点到 18 点营业。", "reply_mode": "ai_guided"}])
    _wire(monkeypatch, sm, kb, _SUPPORT, _FakeStore())
    out = await sm.generate_inbox_draft(
        text=Q, chat_key="peer1", platform="telegram",
        history=[{"role": "user", "content": Q}],
        persona_id="support_zy", conversation_id=_CID, account_id="acc1",
    )
    d = out["kb_decision"]
    assert d["hit"] is True and d["refs"] == 1
    assert "周末 10 点到 18 点营业" in (captured[-1]["kb_context"] or "")
    assert not captured[-1]["nohit"] and kb.misses == []
