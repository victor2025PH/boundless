"""prompt 预算裁剪器门禁（2026-09-11「聊天没有人设、没有记忆」事故沉淀）。

根因：旧 ③ 步按「1 token = 3 字符」截 system 尾部，而 ``_estimate_msg_tokens`` 按
CJK 1 字 = 1 token 计——中文超额被放大 3 倍：11k 的系统提示被砍到 ~700 token，
【后台人设定位】/【用户长期记忆要点】/【自称规则】/【媒体能力边界】整段消失。

门禁钉住：
1. 中文超预算时裁到 [0.85·预算, 预算]，不再「一刀砍穿」；
2. 人设区与硬规则段永不丢；注入尾段按段弹；
3. 注入尾巴丢光仍超 → 软放行（over>0），绝不砍人设；
4. 保底最近几条历史在注入尾巴之后才动；
5. 本地兜底路径（num_ctx 硬上限）同一把尺：二分截尾，不再 ×3 超砍；
6. 分厂商 extra_body / 退役名归一（vendor_params）。
"""
from __future__ import annotations

import pytest

from src.ai.ai_client import AIClient
from src.ai import vendor_params as vp


def _persona_sys(memory_items: int = 8, kb_chars: int = 2500) -> str:
    parts = [
        "你是线上陪伴顾问，语气自然。",
        "【后台人设定位 · 须遵守】\n你是林晚，28 岁，上海人，咖啡师。\n"
        "【身份硬锁·最高优先级】你的名字就是「林晚」。\n" + "背景故事：" + "晚" * 1500
        + "\n\n爱好：" + "咖" * 400,      # 人设块内部含空行（真实 full 块会有）
        "【人称与角色 · 硬规则】对方消息里的「你/你的」都指你自己。",
        "【输出语言】用简体中文回复。",
        "【用户长期记忆要点】\n- 对方叫阿杰，做汽车销售\n- 上周说要去杭州出差\n" + "\n".join(
            f"- 记忆条目{i}：" + "忆" * 120 for i in range(memory_items)),
        "【参考对话示例】\n客户：在吗\n你：在呀" + "例" * 500,
        "【场景状态】现在是傍晚，你刚下班。" + "景" * 300,
        "【知识库参考】\n" + "识" * kb_chars,               # 生产序：KB 在场景之后、硬规则之前
        "【媒体能力边界】你不能发照片/语音，被要求时自然带过。",
        "【自称规则·强制】自称一律用「我」。",
    ]
    return "\n\n".join(parts)


def _msgs(sys_txt: str, rounds: int = 6, cjk: int = 80):
    msgs = [{"role": "system", "content": sys_txt}]
    for i in range(rounds):
        msgs.append({"role": "user", "content": f"旧{i}" + "字" * cjk})
        msgs.append({"role": "assistant", "content": f"答{i}" + "字" * cjk})
    msgs.append({"role": "user", "content": "你今天忙吗"})
    return msgs


def _tokens(msgs) -> int:
    return sum(AIClient._estimate_msg_tokens(m["content"]) for m in msgs)


# ── 1/2/4：中文超预算 → 裁进窄带，人设/硬规则/记忆保住，注入尾段按段弹 ────────

def test_cjk_overflow_trims_into_band_and_keeps_persona():
    msgs = _msgs(_persona_sys(kb_chars=4500), rounds=8, cjk=120)
    before = _tokens(msgs)
    assert before > 10000                                 # 与生产量级同构（~10-12k）
    budget = before - 3000                                # 逼出裁剪
    out, st = AIClient._trim_prompt_to_budget(msgs, budget)
    after = _tokens(out)
    assert st["after"] == after
    assert int(budget * 0.85) <= after <= budget, (before, budget, after, st)
    sys_out = out[0]["content"]
    for must in ("【后台人设定位", "林晚", "晚" * 1500, "咖" * 400, "【人称与角色",
                 "【输出语言】", "【媒体能力边界】", "【自称规则", "【用户长期记忆要点】"):
        assert must in sys_out, must
    assert st["over"] == 0
    # 保底：最近 6 条真实对话还在
    assert sum(1 for m in out if m["role"] != "system") >= 1 + AIClient._HIST_FLOOR_MSGS


def test_kb_and_fewshot_pop_before_memory_and_history_floor():
    msgs = _msgs(_persona_sys(kb_chars=4000), rounds=4)
    before = _tokens(msgs)
    # 只需丢掉 KB+few-shot 就够：记忆/场景/保底历史都不该动
    kb_tok = AIClient._estimate_msg_tokens("【知识库参考】\n" + "识" * 4000)
    fs_tok = AIClient._estimate_msg_tokens("【参考对话示例】\n客户：在吗\n你：在呀" + "例" * 500)
    budget = before - kb_tok - fs_tok + 60
    out, st = AIClient._trim_prompt_to_budget(msgs, budget)
    sys_out = out[0]["content"]
    assert "【参考对话示例】" not in sys_out
    # KB 段被截尾（留头）或整段丢，但绝不越过它去动记忆/场景
    assert "识" * 4000 not in sys_out
    assert "【用户长期记忆要点】" in sys_out and "【场景状态】" in sys_out
    assert st["fewshot"] == 1 and st["inject_chars"] > 0
    assert st["hist"] <= 2          # 最多动了保底之外的最旧一轮
    assert _tokens(out) <= budget


# ── 3：注入尾巴都丢光仍超 → 软放行，人设一个字不少 ───────────────────────────

def test_soft_overflow_never_cuts_persona():
    sys_txt = _persona_sys(memory_items=2, kb_chars=200)
    msgs = _msgs(sys_txt, rounds=2)
    persona_tok = AIClient._estimate_msg_tokens(sys_txt)
    budget = 600                                          # 比人设本体还小
    out, st = AIClient._trim_prompt_to_budget(msgs, budget)
    sys_out = out[0]["content"]
    assert "晚" * 1500 in sys_out and "【后台人设定位" in sys_out and "【自称规则" in sys_out
    assert st["over"] > 0                                 # 明确报告超额，而不是静默砍
    assert st["after"] > budget
    assert [m["role"] for m in out][-1] == "user" and out[-1]["content"] == "你今天忙吗"
    # 旧公式下这里会把 system 砍到 < 1/3：现在人设区完整，长度下限钉住
    assert len(sys_out) >= 1500 + 400


def test_protected_parts_cover_persona_region_with_inner_blank_lines():
    parts = _persona_sys().split("\n\n")
    prot = AIClient._protected_sys_parts(parts)

    def idx(prefix: str) -> int:
        return next(i for i, p in enumerate(parts) if p.startswith(prefix))

    assert 0 in prot
    assert idx("【后台人设定位") in prot
    assert idx("爱好：") in prot                      # 人设区内的第二段也受保护
    assert idx("【人称与角色") in prot
    assert idx("【输出语言】") in prot
    assert idx("【媒体能力边界】") in prot
    assert idx("【自称规则") in prot
    assert idx("【知识库参考】") not in prot
    assert idx("【参考对话示例】") not in prot
    assert idx("【场景状态】") not in prot
    assert idx("【用户长期记忆要点】") not in prot    # 记忆可弹，但在 KB/场景/few-shot 之后


def test_under_budget_untouched_and_zero_budget_off():
    msgs = _msgs(_persona_sys(), rounds=2)
    out, st = AIClient._trim_prompt_to_budget(msgs, 10 ** 6)
    assert out == msgs and st["over"] == 0 and st["inject_chars"] == 0
    out2, _ = AIClient._trim_prompt_to_budget(msgs, 0)
    assert out2 is msgs


def test_default_budget_12000_and_min_yaml_aligned():
    from pathlib import Path
    import yaml
    assert AIClient._DEFAULT_PROMPT_BUDGET == 12000
    p = Path(__file__).resolve().parent.parent / "config" / "config.desktop.min.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert int(data["ai"]["prompt_budget_tokens"]) >= 12000
    assert data["ai"]["model"] == "deepseek-flash"


# ── 5：本地兜底路径同一把尺 ───────────────────────────────────────────────────

def test_local_trim_uses_estimator_not_3x_chars():
    sys_txt = _persona_sys(kb_chars=3000)
    msgs = _msgs(sys_txt, rounds=3)
    budget = 3600
    out = AIClient._trim_messages_to_budget(msgs, budget)
    after = _tokens(out)
    assert after <= budget
    assert after >= int(budget * 0.8), (after, budget)          # 不再一刀砍穿
    sys_out = out[0]["content"]
    assert "【后台人设定位" in sys_out and "林晚" in sys_out
    assert out[-1]["content"] == "你今天忙吗"
    assert len(msgs[0]["content"]) == len(sys_txt)              # 不污染原 messages


def test_local_trim_hard_limit_binary_cut_when_only_persona_left():
    """num_ctx 是硬上限：连人设都放不下时必须截，但按估算器精确截到预算内。"""
    msgs = [{"role": "system", "content": "指" * 3000}, {"role": "user", "content": "在吗"}]
    out = AIClient._trim_messages_to_budget(msgs, 500)
    assert _tokens(out) <= 500
    assert len(out[0]["content"]) >= 500 - 8 - AIClient._estimate_msg_tokens("在吗") - 8


# ── 6：分厂商 extra_body + 退役名归一 ───────────────────────────────────────────

@pytest.mark.parametrize("base,model,expect", [
    ("https://api.deepseek.com/v1", "deepseek-flash", {"thinking": {"type": "disabled"}}),
    ("https://api.deepseek.com", "deepseek-chat", {"thinking": {"type": "disabled"}}),
    ("https://api.deepseek.com/v1", "deepseek-v4-flash", {"thinking": {"type": "disabled"}}),
    ("https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V4-Flash", {"enable_thinking": False}),
    ("https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3.2", {"enable_thinking": False}),
    ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct", {}),
    ("http://100.64.0.173:8001/v1", "chatx", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("http://192.168.1.176:11434/v1", "qwen3:8b", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("https://bd2026.cc/api/ai/v1", "deepseek-flash", {}),      # 托管网关：网关侧注入
    ("https://api.openai.com/v1", "gpt-4o-mini", {}),
])
def test_thinking_off_extra_body_by_host(base, model, expect):
    assert vp.thinking_off_extra_body(base, model) == expect
    assert vp.thinking_off_extra_body(base, model, reasoning=True) == {}


@pytest.mark.parametrize("base,model,expect", [
    ("https://api.deepseek.com/v1", "deepseek-chat", "deepseek-flash"),
    ("https://api.deepseek.com/v1", "deepseek-v4-flash", "deepseek-flash"),
    ("https://api.deepseek.com/v1", "deepseek-v4-flash-vision-exp", "deepseek-flash"),
    ("https://api.deepseek.com/v1", "deepseek-flash", "deepseek-flash"),
    ("https://api.deepseek.com/v1", "deepseek-reasoner", "deepseek-reasoner"),
    ("https://api.siliconflow.cn/v1", "deepseek-chat", "deepseek-chat"),   # 非官方主机不动
    ("https://bd2026.cc/api/ai/v1", "deepseek-chat", "deepseek-chat"),
])
def test_normalize_retired_deepseek_aliases(base, model, expect):
    got, note = vp.normalize_model(base, model)
    assert got == expect
    assert (note is not None) == (got != model)


class _Cfg:
    def __init__(self, ai):
        self._ai = dict(ai)
        self.config = {"ai": dict(ai)}

    def get_ai_config(self):
        return dict(self._ai)


async def test_ai_client_init_normalizes_and_disables_thinking_by_host(monkeypatch, caplog):
    import logging
    import src.ai.ai_client as mod

    class _FakeAsyncOpenAI:
        def __init__(self, **kw):
            self.kw = kw
            self.base_url = kw.get("base_url", "")

    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)

    async def _ok():
        return True

    c = AIClient(_Cfg({
        "provider": "openai_compatible", "base_url": "https://api.deepseek.com",
        "api_key": "sk-x", "model": "deepseek-chat",
        "key_pool": {"enabled": True, "keys": [
            {"name": "sf", "api_key": "sk-sf", "base_url": "https://api.siliconflow.cn/v1",
             "model": "deepseek-ai/DeepSeek-V4-Flash"},
            {"name": "ds2", "api_key": "sk-ds2", "base_url": "https://api.deepseek.com/v1",
             "model": "deepseek-v4-flash"},
        ]},
        "models": {"lan": {"base_url": "http://100.64.0.173:8001", "model": "chatx"}},
    }))
    monkeypatch.setattr(c, "_test_openai_connection", _ok)
    with caplog.at_level(logging.INFO):
        assert await c.initialize() is True
    assert c.model == "deepseek-flash"                       # 退役名归一
    assert c._oa_extra_body["thinking"] == {"type": "disabled"}
    by_name = {e["name"]: e for e in c._pool_entries}
    assert by_name["sf"]["extra_body"] == {"enable_thinking": False}
    assert by_name["ds2"]["model"] == "deepseek-flash"
    assert by_name["ds2"]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert c._route_clients["lan"]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert any("deepseek-chat → deepseek-flash" in r.getMessage() for r in caplog.records)
    assert any("思维链已关闭" in r.getMessage() for r in caplog.records)


async def test_ai_client_reasoning_true_keeps_thinking(monkeypatch):
    import src.ai.ai_client as mod

    class _FakeAsyncOpenAI:
        def __init__(self, **kw):
            self.kw = kw
            self.base_url = kw.get("base_url", "")

    monkeypatch.setattr(mod, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(mod, "OPENAI_SDK_AVAILABLE", True)

    async def _ok():
        return True

    c = AIClient(_Cfg({"provider": "openai_compatible", "base_url": "https://api.deepseek.com/v1",
                       "api_key": "sk-x", "model": "deepseek-flash", "reasoning": True}))
    monkeypatch.setattr(c, "_test_openai_connection", _ok)
    assert await c.initialize() is True
    assert "thinking" not in c._oa_extra_body


def test_chat_ping_payload_and_targets_use_vendor_params(monkeypatch):
    from src.utils import cloud_credentials as cc
    seen = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"usage": {"prompt_tokens": 1, "completion_tokens": 1}}'

    def _fake_urlopen(req, timeout=0):
        import json
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(cc.urllib.request, "urlopen", _fake_urlopen)
    r = cc.probe_chat_key("https://api.siliconflow.cn/v1", "sk-sf", "deepseek-ai/DeepSeek-V4-Flash")
    assert r["ok"] and seen["body"]["enable_thinking"] is False
    cc.probe_chat_key("https://api.deepseek.com/v1", "sk-ds", "deepseek-flash")
    assert seen["body"]["thinking"] == {"type": "disabled"}

    targets = cc.chat_ping_targets({"ai": {
        "api_key": "sk-main", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash",
        "key_pool": {"enabled": True, "keys": [
            {"name": "old", "api_key": "sk-old", "base_url": "https://api.deepseek.com/v1",
             "model": "deepseek-v4-flash"}]}}})
    assert targets[0]["model"] == "deepseek-flash"
