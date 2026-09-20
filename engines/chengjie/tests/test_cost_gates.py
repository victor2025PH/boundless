"""成本门禁（2026-09-08 成本对账 P0）：记忆抽取跳过规则 / 用途归因 / 演练不计账本。"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai import llm_cost as lc  # noqa: E402


def _skip(user_id, chat_id="", ex=None, source=""):
    from src.skills.skill_manager import SkillManager
    return SkillManager._episodic_extract_skip_reason(user_id, chat_id, ex or {}, source=source)


def test_drill_uid_skipped_by_default_and_configurable():
    assert _skip("990001001") == "drill uid"
    assert _skip("8244899900", chat_id="telegram:8244899900:990001006") == "drill uid"
    assert _skip("990001001", ex={"skip_drill": False}) == ""


def test_explicit_skip_chat_ids_match_tail_or_full():
    ex = {"skip_chat_ids": ["-1004345824259", "telegram:6834964252:-1004290740529"]}
    assert _skip("-1004345824259", ex=ex).startswith("chat id in")
    assert _skip("telegram:6834964252:-1004290740529", ex=ex).startswith("chat id in")
    assert _skip("5433982810", ex=ex) == ""


def test_manual_out_on_group_skipped_but_inbound_group_allowed():
    assert _skip("-1004345824259", source="manual_out") == "manual_out on group chat"
    assert _skip("-1004345824259", source="") == ""            # 入站群消息照常
    assert _skip("-1004345824259", source="manual_out", ex={"manual_out_groups": True}) == ""
    assert _skip("5433982810", source="manual_out") == ""      # 私聊 manual_out 照常


def test_skip_reason_never_raises():
    assert _skip(None, None, ex={"skip_chat_ids": 123}) == ""


def test_purpose_explicit_context_and_scope_precedence():
    assert lc.purpose_for_reply({"_llm_purpose": "translate", "chat_id": "990001001"}) == "translate"
    with lc.purpose_scope("tool"):
        assert lc.purpose_for_reply({"chat_id": "990001001"}) == "tool"
        assert lc.purpose_for_reply({"_llm_purpose": "translate"}) == "translate"
    assert lc.purpose_for_reply({"chat_id": "990001001"}) == "drill"
    assert lc.purpose_for_reply({"_llm_purpose": "bogus", "chat_id": "1"}) == "customer_reply"


def test_ai_client_wiring_pinned():
    """源码钉：演练不记 ai_reply；主链/池/本地兜底记账都带 purpose；超时记 suspected。"""
    from src.ai.ai_client import AIClient

    gen = inspect.getsource(AIClient.generate_reply)
    assert "_is_drill" in gen and 'record_action_for_status("ai_reply", 1)' in gen
    oa = inspect.getsource(AIClient._generate_reply_openai_compat)
    assert "purpose=purpose_for_reply(_ctx)" in oa
    assert "_record_suspected_usage(" in oa
    pool = inspect.getsource(AIClient._try_key_pool_chat)
    assert "purpose=purpose_for_reply(context)" in pool
    mem = inspect.getsource(AIClient.extract_memory_facts)
    assert 'purpose="memory_extract"' in mem
    chat = inspect.getsource(AIClient.chat)
    assert 'purpose_scope("tool")' in chat


def test_translation_engine_tags_purpose():
    from src.ai.translation_engines import AIEngine
    assert '"_llm_purpose": "translate"' in inspect.getsource(AIEngine.bare_chat)


def test_assistant_stream_requests_usage():
    src = Path(__file__).resolve().parents[1] / "src" / "web" / "routes" / "assistant_routes.py"
    text = src.read_text(encoding="utf-8")
    assert '"stream_options": {"include_usage": True}' in text
    assert "_record_assistant_usage(" in text
