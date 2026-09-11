"""B1/B2/B3（2026-09-11 用量分析落地）门禁：

B1 工具调用走裸 system：``chat()`` / ``tool_chat()`` 不再把整套人设 / 硬约束 / 语言规则
   拼给抽取、分类、翻译纠错——网关流水实测这类调用占全队 token 16.9%（钧机 46.6%）。
B2 计量只记真回复：``generate_reply`` 只在 purpose == customer_reply 时记 ai_reply；
   工具 / 记忆抽取 / 翻译纠错 / 演练不扣（198 坐席对账：实扣 2,400 vs 真回复 1,777）。
B3 裁剪器保护名单补 LANGUAGE RULE / 多语言回复规则 / 回复硬约束。
本文件不触网（AsyncOpenAI 替身）。
"""
from __future__ import annotations

import inspect

import pytest

from src.ai import llm_purpose as lc
from src.ai.ai_client import AIClient
from src.licensing import token_ledger as tl


class _Cfg:
    config_path = None

    def __init__(self, ai=None, root=None):
        self._ai = dict(ai or {})
        self.config = {"web_admin": {"site_name": "T"}, "ai": dict(self._ai)}
        if root:
            self.config.update(root)

    def get_ai_config(self):
        return dict(self._ai)


class _Msg:
    def __init__(self, content):
        self.content = content
        self.model_extra = {}


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)
        self.finish_reason = "stop"


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _FakeChatClient:
    def __init__(self, reply: str = "ok"):
        self.reply = reply
        self.calls = 0
        self.last_kw = None
        self.base_url = "https://bd2026.cc/api/ai/v1"
        outer = self

        class _Completions:
            async def create(self, **kw):
                outer.calls += 1
                outer.last_kw = kw
                return _Resp(outer.reply)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _client(primary: _FakeChatClient, cfg: _Cfg | None = None) -> AIClient:
    c = AIClient(cfg or _Cfg())
    c._use_openai_compat = True
    c._oa_client = primary
    c.model = "deepseek-flash"
    c._cb_enabled = False
    # 客户回复路径的 system 用替身，避免测试依赖 PersonaManager 全量装配
    c._build_system_instruction = lambda ctx=None: (  # type: ignore[method-assign]
        str((ctx or {}).get("_bare_system") or "") if (ctx or {}).get("_bare_system")
        else "【后台人设定位 · 须遵守】\n你是林晚。\n\n【回复硬约束】\n1. 先回答问题。"
    )
    return c


@pytest.fixture
def billed(monkeypatch):
    """记录 token_ledger.record_action_for_status 被调用的次数（计量出口唯一）。"""
    calls: list = []

    def _rec(action, units, **kw):
        calls.append((action, units))
        return 10

    monkeypatch.setattr(tl, "record_action_for_status", _rec)
    return calls


# ── 用途 API（llm_purpose，不绑 llm_cost / cost_ledger）────────────────────────

def test_purpose_for_reply_priority_and_drill():
    assert lc.purpose_for_reply({"_llm_purpose": "translate", "chat_id": "990001001"}) == "translate"
    with lc.purpose_scope("tool"):
        assert lc.purpose_for_reply({"chat_id": "990001001"}) == "tool"
        assert lc.purpose_for_reply({"_llm_purpose": "translate"}) == "translate"
    assert lc.purpose_for_reply({"chat_id": "990001001"}) == "drill"
    assert lc.purpose_for_reply({"chat_id": "5433982810"}) == "customer_reply"
    assert lc.purpose_for_reply({"_llm_purpose": "bogus", "chat_id": "1"}) == "customer_reply"
    assert lc.purpose_for_reply(None) == "customer_reply"
    assert lc.is_drill_id("tg:990001500")
    assert not lc.is_drill_id("5433982810")


def test_known_purposes_match_website_gateway():
    """引擎白名单必须与官网网关 sanitizePurpose 同表，否则 X-ChatX-Purpose 会被丢掉。"""
    import re
    from pathlib import Path

    ts = Path(__file__).resolve().parents[3] / "website" / "lib" / "ai-gateway.ts"
    if not ts.is_file():
        pytest.skip("website tree not alongside engine")
    text = ts.read_text(encoding="utf-8")
    m = re.search(r"export const KNOWN_PURPOSES = new Set\(\[([\s\S]*?)\]\)", text)
    assert m, "website KNOWN_PURPOSES 块找不到"
    site = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert site == set(lc.KNOWN_PURPOSES)


# ── B1：裸 system ─────────────────────────────────────────────────────────────

async def test_chat_uses_bare_tool_system_and_is_not_billed(billed):
    primary = _FakeChatClient(reply="yes")
    c = _client(primary)
    out = await c.chat("Question: is the customer asking for a photo? Answer yes/no.")
    assert out == "yes"
    msgs = primary.last_kw["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == AIClient.TOOL_SYSTEM_PROMPT
    assert "后台人设定位" not in msgs[0]["content"] and "回复硬约束" not in msgs[0]["content"]
    assert len(msgs) == 2 and msgs[1]["role"] == "user"
    assert billed == []          # 工具调用不记 ai_reply


async def test_tool_chat_custom_system_purpose_not_billed(billed):
    primary = _FakeChatClient(reply='{"facts":[]}')
    c = _client(primary)
    out = await c.tool_chat("USER: hi\nASSISTANT: hello", purpose="memory_extract",
                            system="你是对话记忆抽取器，输出 JSON。")
    assert out == '{"facts":[]}'
    msgs = primary.last_kw["messages"]
    assert msgs[0]["content"] == "你是对话记忆抽取器，输出 JSON。"
    assert billed == []


async def test_chat_respects_outer_purpose_scope_customer_reply_is_billed(billed):
    """主动开场 / 兜底稿经 chat() 出话时显式 purpose_scope("customer_reply") → 记账一次，
    但 system 仍是裸的（那些 prompt 自带角色框定）。"""
    primary = _FakeChatClient(reply="好久不见，最近怎么样？")
    c = _client(primary)
    with lc.purpose_scope("customer_reply"):
        out = await c.chat("你是温暖的线上陪伴，写一句问候。")
    assert out
    assert primary.last_kw["messages"][0]["content"] == AIClient.TOOL_SYSTEM_PROMPT
    assert billed == [("ai_reply", 1)]


async def test_chat_bare_can_be_disabled_by_config(billed):
    primary = _FakeChatClient(reply="yes")
    c = _client(primary, _Cfg(ai={"tool_chat_bare": False}))
    await c.chat("yes or no?")
    sys0 = primary.last_kw["messages"][0]
    # 回旧行为：走完整 system（替身里带人设块），不是裸 system
    assert sys0["role"] == "system" and "后台人设定位" in sys0["content"]
    assert billed == []          # 计量口径不随开关变：tool 用途仍不记


async def test_language_guard_correction_uses_bare_translation_system(billed):
    primary = _FakeChatClient(reply="Hello, nice to meet you.")
    c = _client(primary)
    ctx = {"reply_lang": "en", "_current_user_message_for_lang": "hello there"}
    fixed = await c._guard_reply_language("你好，很高兴认识你，今天过得怎么样呢", ctx)
    assert fixed == "Hello, nice to meet you."
    sys0 = primary.last_kw["messages"][0]["content"]
    assert sys0.startswith("You are a translation tool")
    assert "后台人设定位" not in sys0
    assert billed == []          # 纠错翻译不再第二次扣 ai_reply


async def test_bare_context_skips_spoken_style_tail_and_reply_shaping():
    """裸 system 调用的用户消息原样送出（无 L2 口语化尾注），多行 JSON 输出不被折段。"""
    primary = _FakeChatClient(reply='{\n  "facts": [],\n  "slots": {}\n}')
    c = _client(primary, _Cfg(ai={"spoken_style": {"enabled": True, "level": 2}}))
    out = await c.chat("抽取事实。USER: 我叫小明\nASSISTANT: 你好小明")
    assert primary.last_kw["messages"][1]["content"] == "抽取事实。USER: 我叫小明\nASSISTANT: 你好小明"
    assert out == '{\n  "facts": [],\n  "slots": {}\n}'


# ── B2：计量只记真回复 ───────────────────────────────────────────────────────

async def test_customer_reply_billed_once(billed):
    primary = _FakeChatClient(reply="在的，怎么啦？")
    c = _client(primary)
    out = await c.generate_reply("在吗", {"chat_id": "5433982810", "reply_lang": "zh"})
    assert out
    assert billed == [("ai_reply", 1)]


async def test_drill_and_tool_purposes_not_billed(billed):
    primary = _FakeChatClient(reply="演练回复")
    c = _client(primary)
    await c.generate_reply("在吗", {"chat_id": "990001001", "reply_lang": "zh"})   # 演练号段
    await c.generate_reply("翻译", {"_llm_purpose": "translate", "_bare_system": "translate"})
    with lc.purpose_scope("memory_extract"):
        await c.chat("USER: hi")
    assert billed == []


def test_metering_wiring_pinned():
    gen = inspect.getsource(AIClient.generate_reply)
    assert '_purpose == "customer_reply"' in gen
    assert 'record_action_for_status("ai_reply", 1)' in gen
    chat = inspect.getsource(AIClient.chat)
    assert "_tool_context(" in chat and 'purpose_scope("tool")' in chat
    guard = inspect.getsource(AIClient._guard_reply_language)
    assert '"_bare_system"' in guard and '"_llm_purpose": "tool"' in guard
    hdr = inspect.getsource(AIClient._purpose_header)
    assert "X-ChatX-Purpose" in inspect.getsource(AIClient._generate_reply_openai_compat)
    assert "KNOWN_PURPOSES" in hdr


async def test_customer_reply_sends_purpose_header():
    """B5：客户回复请求带 X-ChatX-Purpose，网关才能按用途分桶。"""
    primary = _FakeChatClient(reply="在的")
    c = _client(primary)
    await c.generate_reply("在吗", {"chat_id": "5433982810", "reply_lang": "zh"})
    assert primary.last_kw["extra_headers"]["X-ChatX-Purpose"] == "customer_reply"


async def test_tool_chat_sends_tool_purpose_header():
    primary = _FakeChatClient(reply="yes")
    c = _client(primary)
    await c.chat("yes or no?")
    assert primary.last_kw["extra_headers"]["X-ChatX-Purpose"] == "tool"


# ── B3：裁剪保护名单 ─────────────────────────────────────────────────────────

def _sys_with_language_rule(rule_chars: int = 2700) -> str:
    parts = [
        "【后台人设定位 · 须遵守】\n你是美月·伊莎贝拉，33 岁。" + "背景" * 600,
        "【身份硬锁】你就是真人「美月·伊莎贝拉」。",
        "【回复硬约束】\n1. 先正面回答。\n4. 严禁用 () 或 [] 描写动作/情绪。" + "约" * 900,
        "【人称与角色 · 硬规则】对方消息里的「你」指你自己。",
        "【深度人设增强】" + "忆" * 800,
        "【此刻】现在大约是周四晚上。",
        "【LANGUAGE RULE — TOP PRIORITY — MANDATORY】\nACTIVE CONVERSATION LANGUAGE: English. "
        + "You MUST reply ENTIRELY in English. " * (rule_chars // 40),
        "【说话指纹】温柔慢半拍。" + "纹" * 200,
    ]
    return "\n\n".join(parts)


def test_language_rule_and_hard_constraints_survive_trimming():
    sys_txt = _sys_with_language_rule()
    msgs = [{"role": "system", "content": sys_txt}]
    for i in range(8):
        msgs.append({"role": "user", "content": f"old{i} " + "message text " * 20})
        msgs.append({"role": "assistant", "content": f"reply{i} " + "answer text " * 20})
    msgs.append({"role": "user", "content": "Good morning! How was your night?"})
    before = sum(AIClient._estimate_msg_tokens(m["content"]) for m in msgs)
    out, st = AIClient._trim_prompt_to_budget(msgs, before - 1500)
    sys_out = out[0]["content"]
    assert "【LANGUAGE RULE" in sys_out, st
    assert "【回复硬约束】" in sys_out and "严禁用 () 或 []" in sys_out
    assert "【后台人设定位" in sys_out
    # 注入尾巴（深度人设增强 / 说话指纹）才是该丢的
    assert st["inject_chars"] > 0 or st["hist"] > 0


def test_protected_heads_pinned():
    heads = AIClient._PROTECTED_SYS_HEADS
    for must in ("【LANGUAGE RULE", "【多语言回复规则", "【回复硬约束】", "【后台人设定位", "【输出语言"):
        assert must in heads
