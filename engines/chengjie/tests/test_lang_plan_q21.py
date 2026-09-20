"""Q-21 B/C（#302 Y82GWM / #290，2026-09-12）：起草前会话语言计划 + lang_unknown 不再静默 + 粤语生成链。

指令验收口径（docs/指令_Q-21_翻译界面三件_下拉可见与粤语与字体_2026-09-11.md §E）：
- 英文「Hi」× 中文人设 × 全自动 → ``reply_lang=en``、``decided_by=message``（文字系统兜底归并进
  「客户消息证据」，原始来源留在 ``via=message_script``），一行 ``[lang-plan]`` 日志；
- lang_unknown（对方零证据）× 全自动 → 按人设语言起草（``decided_by=persona``、``fallback=True``），
  l1_reason 判 lang_unknown 与计划同源；计划已判「对方语言有证据」时 l1 不得再报 lang_unknown；
- ``resolve_outbound_lang`` 六级顺序不动（D-M3 红线；manual 仍最高，计划不得推翻 manual）；
- 粤语：``yue_style_directive("yue")`` 出口语粤语指令 + 3 条 few-shot，其它语种空串；``ai_client._LANG_NAMES``
  有 yue / zh-tw（否则 LANGUAGE RULE 直接短路不写）；翻译路由跳过 ``supports_target(yue)=False`` 的引擎。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from src.inbox import l1_reason
from src.inbox import outbound_translate as ot


class _Store:
    """最小假会话库：只给 resolve_outbound_lang 用到的三个方法。"""

    def __init__(self, msgs: List[Dict[str, Any]], manual: str = "", conv: Dict[str, Any] | None = None):
        self._msgs = msgs
        self._manual = manual
        self._conv = conv or {}

    def list_recent_messages(self, cid: str, limit: int = 20):
        return list(self._msgs)[-limit:]

    def get_conversation(self, cid: str):
        return dict(self._conv)

    def get_outbound_lang_if_set(self, cid: str):
        return self._manual


def _no_detect(text: str) -> str:
    # 统计检测判不出（短句 / 表情），逼出文字系统兜底 / 人设兜底两条路
    return ""


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    ot._reset_plan_registry_for_tests()
    # 账号先验（lang_prior）在测试里恒空，避免它掩盖「人设兜底」路径
    import src.ai.lang_prior as lp
    monkeypatch.setattr(lp, "initial_lang_hint", lambda **kw: "", raising=False)
    yield
    ot._reset_plan_registry_for_tests()


def _persona(monkeypatch, lang: str):
    monkeypatch.setattr(ot, "persona_default_lang", lambda cfg, p, a, c: lang)


# ── B1：英文「Hi」× 中文人设 × 全自动 → en / message ────────────────────────────
def test_hi_english_with_zh_persona_plans_en_by_message(monkeypatch, caplog):
    _persona(monkeypatch, "zh")
    store = _Store([{"direction": "in", "text": "Hi"}])
    with caplog.at_level(logging.INFO):
        plan = ot.build_conv_lang_plan(
            "messenger:acc1:peer1", store=store, cfg_root={"x": 1}, detect=_no_detect)
    assert plan.reply_lang == "en"
    assert plan.decided_by == "message"          # 指令口径
    assert plan.via == "message_script"          # 原始来源可追
    assert plan.xlate_target == "en" and plan.tts_lang == "en"
    assert plan.peer_known and not plan.fallback
    assert plan.persona_lang == "zh"
    line = [r.getMessage() for r in caplog.records if "[lang-plan]" in r.getMessage()]
    assert len(line) == 1, line
    assert "conv=messenger:acc1:peer1" in line[0] and "reply=en" in line[0]
    assert "by=message" in line[0] and "via=message_script" in line[0] and "fallback=0" in line[0]
    # 注册表可读，供 /api/drafts、会话头 chip、l1_reason
    peek = ot.peek_conv_lang_plan("messenger:acc1:peer1")
    assert peek and peek["reply_lang"] == "en" and peek["decided_by"] == "message" and peek["peer_known"] is True
    # l1_reason 与计划同源：对方语言有证据 → 不是 lang_unknown
    assert l1_reason._lang_unknown("Hi", {"conversation_id": "messenger:acc1:peer1", "language": ""}) is False


# ── B2：对方零证据 × 全自动 → 按人设语言起草 + fallback 标注（不再静默 L1）─────────
def test_lang_unknown_falls_back_to_persona_language_with_annotation(monkeypatch, caplog):
    _persona(monkeypatch, "en")
    store = _Store([{"direction": "in", "text": "[图片]"}])
    with caplog.at_level(logging.INFO):
        plan = ot.build_conv_lang_plan(
            "whatsapp:acc2:peer2", store=store, cfg_root={"x": 1}, detect=_no_detect)
    assert plan.reply_lang == "en"                 # 按人设语言起草，而不是 ""→旧 default（zh）
    assert plan.decided_by == "persona" and plan.via == "persona"
    assert plan.fallback is True and plan.peer_known is False
    d = plan.as_dict()
    assert d["fallback"] is True and d["persona_lang"] == "en"
    line = [r.getMessage() for r in caplog.records if "[lang-plan]" in r.getMessage()][0]
    assert "by=persona" in line and "fallback=1" in line
    # 人审档：l1_reason 仍判 lang_unknown（原因可见），与计划同源
    assert l1_reason._lang_unknown("[图片]", {"conversation_id": "whatsapp:acc2:peer2", "language": ""}) is True


def test_no_persona_no_evidence_is_unknown_plan_not_zh():
    store = _Store([{"direction": "in", "text": "[图片]"}])
    plan = ot.build_conv_lang_plan("telegram:acc3:peer3", store=store, cfg_root={}, detect=_no_detect, log=False)
    assert plan.reply_lang == "" and plan.decided_by == "unknown"
    assert plan.fallback is False                  # 没语言可回落 → 不标「按人设回」，产线走旧 default
    assert plan.confidence == 0.0


# ── B3：红线——六级顺序不动；manual 最高，明确请求不得推翻 manual ──────────────────
def test_manual_still_wins_over_explicit_request(monkeypatch):
    _persona(monkeypatch, "zh")
    store = _Store([{"direction": "in", "text": "please speak japanese"}], manual="th")
    plan = ot.build_conv_lang_plan(
        "messenger:acc:p", store=store, cfg_root={"x": 1}, detect=_no_detect,
        history=[{"role": "user", "content": "please speak japanese"}], log=False)
    assert plan.reply_lang == "th" and plan.decided_by == "manual" and plan.confidence == 1.0


def test_explicit_request_overrides_script_guess(monkeypatch):
    _persona(monkeypatch, "zh")
    store = _Store([{"direction": "in", "text": "please speak japanese"}])
    plan = ot.build_conv_lang_plan(
        "messenger:acc:p2", store=store, cfg_root={"x": 1}, detect=_no_detect,
        history=[{"role": "user", "content": "please speak japanese"}], log=False)
    if plan.decided_by == "request":
        assert plan.reply_lang == "ja" and plan.peer_known
    else:   # lang_policy 未识别该句式时退回文字系统兜底，仍是「客户消息证据」
        assert plan.decided_by == "message" and plan.reply_lang == "en"


def test_resolve_outbound_lang_six_level_order_unchanged():
    import inspect
    src = inspect.getsource(ot.resolve_outbound_lang)
    returns = ('return t, "manual"', 'return prof, "profile"', 'return msg, "message"',
               'return sc, "message_script"', 'return hist, "outbound_history"', 'return persona, "persona"')
    pos = [src.index(x) for x in returns]
    assert pos == sorted(pos), "resolve_outbound_lang 六级返回顺序被改动（D-M3 红线）"


# ── B4：产线调用点——autodraft_helpers 起草前建计划并把 reply_lang 传给 generate_persona_reply ─
def test_autodraft_helpers_wires_plan_into_generate_persona_reply():
    import inspect
    from src.inbox import autodraft_helpers as ah
    src = inspect.getsource(ah)
    assert "build_conv_lang_plan(" in src
    assert src.count("reply_lang=_plan_lang") >= 2, "主路径 + 回声守卫重试都要吃计划语言"
    # l1_reason 之前先登记计划（同源）
    i_plan = src.index("build_conv_lang_plan(", src.index("derive_l1_reason") - 2500)
    assert i_plan < src.index("derive_l1_reason(")


def test_drafts_api_attaches_lang_plan():
    import inspect
    from src.web.routes import drafts_routes
    assert 'd["lang_plan"] = ' in inspect.getsource(drafts_routes)


# ── C：粤语生成链 ───────────────────────────────────────────────────────────────
def test_yue_style_directive_only_for_cantonese():
    from src.inbox.persona_reply import yue_style_directive
    d = yue_style_directive("yue")
    assert "粵語" in d and "繁體字" in d
    assert d.count("\n") >= 3 or d.count("→") >= 3       # 3 条 few-shot
    for bad in ("zh", "zh-tw", "en", "", None):
        assert yue_style_directive(bad) == ""
    assert yue_style_directive("zh-yue") == d and yue_style_directive("YUE") == d


def test_ai_client_lang_names_know_yue_and_zh_tw():
    import inspect
    import re
    from src.ai import ai_client
    src = inspect.getsource(ai_client)
    m = re.search(r"_LANG_NAMES = \{(.*?)\n\s*\}", src, re.S)
    assert m, "ai_client 缺 _LANG_NAMES"
    assert re.search(r'"yue":\s*"[^"]*粵語', m.group(1)), "缺 yue → LANGUAGE RULE 对粤语短路"
    assert '"zh-tw":' in m.group(1)


def test_persona_reply_injects_yue_directive_in_all_three_paths():
    import inspect
    from src.inbox import persona_reply
    src = inspect.getsource(persona_reply)
    assert src.count("yue_style_directive(") >= 4   # 定义 + 统一路径 extra_hint + 兜底 ai.chat + 开场白


@pytest.mark.asyncio
async def test_translate_router_skips_engines_without_yue_support():
    from src.ai.translation_engines import EngineResult, EngineRouter

    calls: List[str] = []

    class _Eng:
        available = True

        def __init__(self, name, ok_targets):
            self.name = name
            self._ok = ok_targets

        def supports_target(self, t):
            return t in self._ok

        async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
            calls.append(self.name)
            return EngineResult("譯文", self.name, True, "")

    router = EngineRouter([_Eng("deepl", {"en", "zh"}), _Eng("google", {"en", "zh", "yue"})])
    res = await router._translate_pick("你好", source_lang="zh", target_lang="yue")
    assert res.ok and res.engine == "google"
    assert calls == ["google"], "不支持 yue 的引擎必须让路，而不是先失败再切"
    # 全体不支持 → ok=False，错误里点名 unsupported_target
    calls.clear()
    router2 = EngineRouter([_Eng("deepl", {"en"}), _Eng("opencc", {"zh-tw"})])
    res2 = await router2._translate_pick("你好", source_lang="zh", target_lang="yue")
    assert not res2.ok and "unsupported_target:yue" in (res2.error or "") and calls == []
