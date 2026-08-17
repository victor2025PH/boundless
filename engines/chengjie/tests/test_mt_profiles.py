# -*- coding: utf-8 -*-
"""MT 模型契约档门禁（纯函数 + 假客户端，无网络无 GPU，常驻 CI）。

守两类不变量：

1. **默认档零漂移**——`hunyuan_mt` 的 prompt 文本、语种白名单、调用模式必须与抽档
   之前逐字一致。这套 prompt 是生产在跑的出站翻译，动一个字都要有人显式决定。
2. **换档是整套换**——补全式模型必须同时换到 completions 口、换到自己的语种集、
   用自己的语种命名。任何一项漏换都不会报错，只会让新模型「测起来不如旧的」。
"""

from __future__ import annotations

import pytest

from src.ai.mt_profiles import (
    DEFAULT_PROFILE,
    HYMT_LANGS,
    MILMMT_LANGS,
    get_profile,
    guess_profile,
)
from src.ai.translation_engines import OllamaMTEngine


# ── 默认档：与抽档前逐字一致 ──────────────────────────────────────────────
def test_hunyuan_prompt_text_is_frozen():
    p = get_profile("hunyuan_mt")
    assert p.mode == "chat"
    assert p.build_prompt("hello", "en", "zh") == \
        "把下面的文本翻译成中文，不要额外解释。\n\nhello"
    assert p.build_prompt("你好", "zh", "th") == \
        "把下面的文本翻译成泰语，不要额外解释。\n\n你好"
    assert p.build_prompt("hola", "es", "en") == \
        "Translate the following segment into English, without additional explanation.\n\nhola"


def test_engine_defaults_to_hunyuan_profile():
    e = OllamaMTEngine("http://h:11434", "hy-mt2")
    assert e.profile.name == DEFAULT_PROFILE == "hunyuan_mt"
    assert e.supports_target("uk") is True     # HY 有
    assert e.supports_target("sw") is False    # 集外让位下一引擎
    assert e.supports_target("") is False


# ── 语种集：两个模型不是包含关系，这正是「白名单必须跟模型走」的理由 ──────
def test_language_sets_are_not_nested():
    only_milmmt = MILMMT_LANGS - HYMT_LANGS
    only_hymt = HYMT_LANGS - MILMMT_LANGS
    # MiLMMT 多出的语种若被旧白名单拦住，换模型将拿不到任何覆盖收益
    assert {"az", "bg", "sv", "zht", "el", "fi"} <= only_milmmt
    # 且它确实少了四种——换模型是有代价的取舍，不是纯升级
    assert only_hymt == {"te", "mr", "gu", "uk"}


def test_milmmt_profile_language_naming_and_aliases():
    p = get_profile("milmmt46")
    # 简繁按两个语种训练，名字必须照抄模型卡
    assert "Chinese (Simplified)" in p.build_prompt("x", "en", "zh")
    assert "Chinese (Traditional)" in p.build_prompt("x", "en", "zht")
    # 常见等价码归一（no→nb / zh-TW→zht），否则会被当未知语种拒掉
    assert p.supports("no") and p.supports("zh-TW") and p.supports("fil")
    assert not p.supports("sw")


# ── 补全式契约 ────────────────────────────────────────────────────────────
def test_milmmt_prompt_is_completion_style():
    p = get_profile("milmmt46")
    assert p.mode == "completion"
    assert p.build_prompt("我爱机器翻译", "zh", "en") == (
        "Translate this from Chinese (Simplified) to English:\n"
        "Chinese (Simplified): 我爱机器翻译\n"
        "English:"
    )


def test_milmmt_prompt_without_source_lang_does_not_invent_one():
    # 源语未知时编一个语种名会把模型引到错误的语言先验，宁可退化为最小变体
    out = get_profile("milmmt46").build_prompt("hola", "", "en")
    assert "Translate this to English:" in out and "None" not in out


def test_milmmt_postprocess_strips_echoed_anchor():
    p = get_profile("milmmt46")
    assert p.postprocess("English: I love MT", "en") == "I love MT"
    assert p.postprocess("I love MT", "en") == "I love MT"


# ── 档名解析：笔误不该让出站翻译整条崩 ────────────────────────────────────
def test_unknown_profile_falls_back_with_warning(caplog):
    with caplog.at_level("WARNING"):
        assert get_profile("no-such-model").name == DEFAULT_PROFILE
    assert "未知 MT 档案" in caplog.text
    assert get_profile("").name == DEFAULT_PROFILE
    assert get_profile("MiLMMT-46").name == "milmmt46"   # 别名 + 大小写


def test_guess_profile_from_model_name():
    assert guess_profile("hf.co/x/MiLMMT-46-4B-v0.1-GGUF:Q4_K_M") == "milmmt46"
    assert guess_profile("hy-mt2-7b-official:latest") == "hunyuan_mt"


# ── 引擎接线：换档必须真的换调用口 ────────────────────────────────────────
def _fake_completion_cli(text: str = "I love MT"):
    class _Choice:
        def __init__(self):
            self.text = text

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        kwargs = None
        calls = 0

        async def create(self, **kw):
            _Completions.kwargs = kw
            _Completions.calls += 1
            return _Resp()

    class _Chat:
        class completions:  # noqa: N801  chat 口若被误用，调用即炸
            @staticmethod
            async def create(**kw):
                raise AssertionError("补全式模型不该走 chat.completions")

    class _Cli:
        def __init__(self):
            self.completions = _Completions()
            self.chat = _Chat()

    return _Cli()


@pytest.mark.asyncio
async def test_completion_profile_uses_completions_endpoint():
    e = OllamaMTEngine("http://h:11434", "milmmt-46-4b", profile="milmmt46")
    cli = _fake_completion_cli("English: I love MT")
    e._clients["http://h:11434"] = cli
    r = await e.translate("我爱机器翻译", source_lang="zh", target_lang="en")
    assert r.ok and r.engine == "ollama_mt"
    assert r.text == "I love MT"          # 锚点回显被 postprocess 剥掉
    kw = cli.completions.kwargs
    assert "messages" not in kw           # 没走 chat 口
    assert kw["prompt"].endswith("English:")
    assert kw["model"] == "milmmt-46-4b"


@pytest.mark.asyncio
async def test_completion_profile_respects_its_own_lang_set():
    e = OllamaMTEngine("http://h:11434", "milmmt-46-4b", profile="milmmt46")
    # HY 白名单没有瑞典语；换档后必须放行，否则换模型等于零覆盖收益
    assert e.supports_target("sv") is True
    # 反向：MiLMMT 没训乌克兰语，即便 HY 有也必须让位
    r = await e.translate("你好", source_lang="zh", target_lang="uk")
    assert not r.ok and "unsupported_target" in r.error


# ── 多实例：主力 MT + 补位 MT 共存 ────────────────────────────────────────
def test_build_engines_registers_second_local_mt_under_its_own_name():
    from src.ai.translation_engines import build_engines

    eng = build_engines({"engines": {
        "order": ["ollama_mt", "milmmt", "ai"],
        "ollama_mt": {"base_url": "http://h:11434", "model": "hy-mt2"},
        "milmmt": {"type": "ollama_mt", "base_url": "http://h:11434",
                   "model": "milmmt-4b", "profile": "milmmt46"},
    }}, ai_client=None)
    assert [e.name for e in eng] == ["ollama_mt", "milmmt", "ai"]
    primary, backup = eng[0], eng[1]
    assert primary.profile.name == "hunyuan_mt"
    assert backup.profile.name == "milmmt46"
    # 分工成立：瑞典语主力不收、补位收；乌克兰语反过来
    assert (primary.supports_target("sv"), backup.supports_target("sv")) == (False, True)
    assert (primary.supports_target("uk"), backup.supports_target("uk")) == (True, False)


def test_unknown_order_entry_without_type_is_still_ignored():
    from src.ai.translation_engines import build_engines

    # 没有 type 声明的未知名 = 笔误，必须继续被忽略（不能悄悄装一个空引擎）
    eng = build_engines({"engines": {
        "order": ["typo_engine", "ai"],
        "typo_engine": {"model": "whatever"},
    }}, ai_client=None)
    assert [e.name for e in eng] == ["ai"]


def test_default_ollama_instance_keeps_canonical_name():
    from src.ai.translation_engines import build_engines

    eng = build_engines({"engines": {
        "order": ["ollama_mt"],
        "ollama_mt": {"base_url": "http://h:11434", "model": "hy-mt2"},
    }}, ai_client=None)
    # 统计/per_lang_order/日志全按引擎名寻址，默认名漂移会静默打断既有配置
    assert eng[0].name == "ollama_mt"
