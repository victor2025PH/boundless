"""免费档（"" / std / free）只能走零成本引擎（智语）。

每条曾核实的泄漏路径都有对应用例：std 打不到付费引擎，pro / certified
仍打得到。分类按生产类，不按桩的 name。
"""
import inspect

import pytest

from src.ai.translation_engines import (
    AIEngine,
    DeepLEngine,
    EngineResult,
    EngineRouter,
    OllamaMTEngine,
    OpenAICompatEngine,
    build_engines,
    engine_blocked_for_tier,
    free_tier_guard_enabled,
    is_paid_engine,
)
from src.ai.translation_engine_stats import get_translation_engine_stats
from src.ai.translation_fusion import fuse_compare_candidates
from src.ai.translation_service import TranslationService


@pytest.fixture(autouse=True)
def _reset_paid_blocked_stats():
    get_translation_engine_stats().reset()
    yield
    get_translation_engine_stats().reset()


@pytest.fixture(autouse=True)
def _wallet_does_not_degrade(monkeypatch):
    """付费档用例不被空钱包降成 std。降级专例自己再打补丁。"""
    monkeypatch.setattr(
        "src.licensing.token_ledger.should_degrade_action",
        lambda *a, **k: False,
    )


class _Chat:
    def __init__(self, text="你好朋友"):
        self.text = text
        self.calls = 0

    async def chat(self, prompt, ctx=None):
        self.calls += 1
        return self.text

    async def generate_reply(self, prompt, **kwargs):
        self.calls += 1
        return self.text


class _RecAI(AIEngine):
    def __init__(self, text="你好朋友"):
        self.client = _Chat(text)
        super().__init__(self.client)
        self.calls = 0

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        self.calls += 1
        return EngineResult(self.client.text, self.name, True)


class _RecDeepL(DeepLEngine):
    def __init__(self, outs=None):
        super().__init__("test-key")
        self.calls = 0
        self._outs = list(outs or ["你好"])

    @property
    def available(self):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        i = min(self.calls, len(self._outs) - 1)
        self.calls += 1
        return EngineResult(self._outs[i], self.name, True)


class _Local:
    """零成本桩（属性 zero_cost，不是生产类）。"""

    def __init__(self, text="你好", *, fail=False, outs=None, name="ollama_mt"):
        self.name = name
        self.zero_cost = True
        self.available = True
        self.calls = 0
        self._text = text
        self._fail = fail
        self._outs = list(outs) if outs is not None else None

    def supports_target(self, target_lang):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        self.calls += 1
        if self._fail:
            return EngineResult("", self.name, False, "timeout")
        if self._outs is not None:
            i = min(self.calls - 1, len(self._outs) - 1)
            return EngineResult(self._outs[i], self.name, True)
        return EngineResult(self._text, self.name, True)


class _CoolingMT(OllamaMTEngine):
    """端点冷却后的失败形态：引擎自己返回失败，路由不得改打云端 LLM。"""

    def __init__(self):
        super().__init__("http://127.0.0.1:9", "hy-mt")
        self.calls = 0

    @property
    def available(self):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        self.calls += 1
        return EngineResult("", self.name, False, "timeout")


def _svc(*engines, **kw):
    """本文件里没写开关的服务 = enforce，专门钉「拦住泄漏」。默认构造另有用例。"""
    kw.setdefault("free_tier_zero_cost", True)
    return TranslationService(engines=list(engines), **kw)


# ── 分类与配置 ──────────────────────────────────────────────────────────────

def test_production_engine_classes_are_classified():
    import src.ai.translation_engines as te
    skip = {"EngineResult", "EngineRouter"}
    seen = []
    for name, cls in inspect.getmembers(te, inspect.isclass):
        if cls.__module__ != te.__name__ or name in skip:
            continue
        if not callable(getattr(cls, "translate", None)):
            continue
        seen.append(name)
        assert cls in te.ZERO_COST_ENGINE_CLASSES or cls in te.PAID_ENGINE_CLASSES, name
    assert {"AIEngine", "DeepLEngine", "OllamaMTEngine", "OpenCCEngine",
            "OpenAICompatEngine"} <= set(seen)


def test_guard_switch_and_ollama_openai_mode_stays_zero_cost():
    assert free_tier_guard_enabled(None) is False
    assert free_tier_guard_enabled("legacy") is False
    assert free_tier_guard_enabled("off") is False
    assert free_tier_guard_enabled("enforce") is True
    assert free_tier_guard_enabled(True) is True
    assert free_tier_guard_enabled(False) is False
    assert TranslationService().free_tier_zero_cost is False
    mt = OllamaMTEngine("http://127.0.0.1:9", "m", api="openai")
    assert is_paid_engine(mt) is False
    built = build_engines(
        {"engines": {"order": ["hunyuan_mt"],
                     "ollama_mt": {"base_url": "http://127.0.0.1:9", "model": "m"}}},
        None,
    )
    assert isinstance(built[0], OllamaMTEngine)
    assert is_paid_engine(built[0]) is False


def test_config_example_and_web_app_wire_the_switch():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    example = (root / "config" / "config.example.yaml").read_text(encoding="utf-8")
    assert "free_tier_zero_cost: legacy" in example
    assert "free_tier_zero_cost: enforce" in example
    web = (root / "src" / "bootstrap" / "web_app.py").read_text(encoding="utf-8")
    assert "free_tier_zero_cost" in web
    assert "free_tier_guard_enabled" in web


def test_stub_named_like_paid_engine_is_not_blocked_but_paid_flag_is():
    """测试桩不按 name 拦截；显式 paid=True 会拦截。"""

    class _Stub:
        def __init__(self):
            self.name = "deepl"
            self.available = True
            self.calls = 0

        def supports_target(self, target_lang):
            return True

        async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
            self.calls += 1
            return EngineResult("你好", self.name, True)

    stub = _Stub()
    assert engine_blocked_for_tier(stub, "") is False
    svc = TranslationService(engine_router=EngineRouter([stub]))
    # 同步跑不了，放到下面的 async 用例。这里只钉分类。
    stub.paid = True
    assert engine_blocked_for_tier(stub, "") is True
    assert engine_blocked_for_tier(stub, None) is False


@pytest.mark.asyncio
async def test_named_stub_still_runs_on_std_and_direct_router_omits_guard():
    class _Stub:
        def __init__(self):
            self.name = "deepl"
            self.available = True
            self.calls = 0

        def supports_target(self, t):
            return True

        async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
            self.calls += 1
            return EngineResult("你好", self.name, True)

    stub = _Stub()
    svc = TranslationService(engine_router=EngineRouter([stub]))
    r = await svc.translate("hello friend", target_lang="zh", source_lang="en")
    assert r.ok and stub.calls == 1 and r.provider == "deepl"

    ai = _RecAI()
    direct = await EngineRouter([ai]).translate(
        "hello friend", source_lang="en", target_lang="zh")
    assert direct.ok and ai.calls == 1  # tier 缺省 None：路由单测不设防


# ── 泄漏 1/2：failover、默认 order、per_lang 尾部追加 ────────────────────────

@pytest.mark.asyncio
async def test_std_failover_and_default_order_never_call_ai():
    ai = _RecAI()
    local = _Local(fail=True)
    r = await _svc(local, ai).translate(
        "hello friend", target_lang="zh", source_lang="en")
    assert ai.calls == 0 and local.calls == 1
    assert r.ok is False and r.free_tier_blocked and r.needs_human
    assert r.translated_text == "hello friend"
    assert "timeout" in r.error  # 零成本失败保留原错误，不盖成守卫文案

    bare = _RecAI()
    engines = build_engines({}, bare.client)
    assert any(isinstance(e, AIEngine) for e in engines)
    r2 = await TranslationService(engines=engines, free_tier_zero_cost=True).translate(
        "hello friend", target_lang="zh", source_lang="en")
    assert bare.client.calls == 0
    assert r2.ok is False and r2.error == "free_tier_no_zero_cost_engine"
    assert r2.needs_human is True
    stats = get_translation_engine_stats().dump()
    assert stats["paid_blocked"].get("ai", 0) >= 1
    assert stats["paid_would_block_total"] == 0
    assert 'translation_engine_paid_blocked_total{engine="ai"}' in (
        get_translation_engine_stats().dump_prom())


@pytest.mark.asyncio
async def test_per_lang_tail_does_not_append_paid_call_for_std():
    local = _Local(fail=True)
    ai = _RecAI()
    router = EngineRouter([local, ai], per_lang_order={"en": ["ollama_mt"]})
    svc = TranslationService(engine_router=router, free_tier_zero_cost=True)
    r = await svc.translate("hello friend", target_lang="en", source_lang="zh")
    assert local.calls == 1 and ai.calls == 0
    assert r.free_tier_blocked and r.needs_human
    assert "timeout" in r.error


@pytest.mark.asyncio
async def test_std_tiers_block_and_paid_tiers_reach_engines():
    ai = _RecAI()
    for tier in ("", "std", "free"):
        ai.calls = 0
        r = await _svc(ai).translate(
            "hello friend", target_lang="zh", source_lang="en", tier=tier)
        assert ai.calls == 0 and r.error == "free_tier_no_zero_cost_engine", tier

    ai.calls = 0
    r = await _svc(ai).translate(
        "hello friend", target_lang="zh", source_lang="en", tier="pro")
    assert r.ok and r.provider == "ai" and ai.calls == 1 and r.needs_human is False

    ai.calls = 0
    r = await _svc(ai).translate(
        "hello friend", target_lang="zh", source_lang="en", tier="enterprise")
    assert r.ok and ai.calls == 1  # 未知显式档 fail-open，不当免费档


@pytest.mark.asyncio
async def test_default_is_legacy_and_counts_would_block_without_skipping():
    """缺省构造 = legacy：免费请求仍打到付费引擎，同时记 would_block。"""
    ai = _RecAI()
    local = _Local(fail=True)
    svc = TranslationService(engines=[local, ai])
    assert svc.free_tier_zero_cost is False
    r = await svc.translate("hello friend", target_lang="zh", source_lang="en")
    assert r.ok and r.provider == "ai" and ai.calls == 1 and local.calls == 1
    assert r.free_tier_blocked is False and r.needs_human is False
    stats = get_translation_engine_stats().dump()
    assert stats["paid_would_block"].get("ai", 0) >= 1
    assert stats["paid_blocked_total"] == 0
    assert 'translation_engine_paid_would_block_total{engine="ai"}' in (
        get_translation_engine_stats().dump_prom())
    # 付费档不记 would_block，引擎照旧可达。
    before = stats["paid_would_block_total"]
    ai.calls = 0
    pro = await svc.translate(
        "good morning friend", target_lang="zh", source_lang="en", tier="pro")
    assert pro.ok and pro.provider == "ai" and ai.calls == 1
    assert get_translation_engine_stats().dump()["paid_would_block_total"] == before
    # legacy 的换引擎重试仍能选到付费引擎；真正调用时再记 would_block。
    assert svc.next_engine_after("ollama_mt", "zh") == "ai"


@pytest.mark.asyncio
async def test_legacy_switch_keeps_std_on_the_old_engine_chain():
    ai = _RecAI()
    r = await _svc(ai, free_tier_zero_cost=False).translate(
        "hello friend", target_lang="zh", source_lang="en")
    assert r.ok and r.provider == "ai" and ai.calls == 1
    assert r.free_tier_blocked is False
    assert get_translation_engine_stats().dump()["paid_would_block"].get("ai", 0) >= 1


# ── 泄漏 3：会话首选引擎 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pref_engine_blocked_for_std_and_used_for_pro():
    local = _Local()
    deepl = _RecDeepL()
    std = await _svc(deepl, local).translate(
        "hello friend", target_lang="zh", source_lang="en", engine="deepl")
    assert deepl.calls == 0 and local.calls == 1
    assert std.ok and std.provider == "ollama_mt"
    assert get_translation_engine_stats().dump()["paid_blocked"].get("deepl", 0) >= 1

    deepl.calls = 0
    pro = await _svc(deepl, local).translate(
        "hello friend", target_lang="zh", source_lang="en", engine="deepl", tier="pro")
    assert pro.ok and pro.provider == "deepl" and deepl.calls == 1


# ── 泄漏 4：出站重试 next_engine_after / retry_once ─────────────────────────

@pytest.mark.asyncio
async def test_retry_and_next_engine_skip_paid_on_std():
    local = _Local(fail=True)
    ai = _RecAI()
    svc = _svc(local, ai)
    first = await svc.translate("hello friend", target_lang="zh", source_lang="en")
    assert first.ok is False and ai.calls == 0
    assert svc.next_engine_after("ollama_mt", "zh") == ""
    assert svc.next_engine_after("ollama_mt", "zh", tier="pro") == "ai"
    again = await svc.retry_once(
        "hello friend", target_lang="zh", source_lang="en", engine="ai")
    assert ai.calls == 0
    assert again.ok is False and again.free_tier_blocked and again.needs_human
    assert "free_tier_paid_engine_blocked" in again.error

    paid = await svc.retry_once(
        "hello friend", target_lang="zh", source_lang="en", engine="ai", tier="pro")
    assert paid.ok and paid.provider == "ai" and ai.calls == 1


# ── 泄漏 5：汉字残留重译复用胜出引擎 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_cjk_residue_retries_zero_cost_winner_not_paid():
    local = _Local(outs=['Which app is "智聊"?', 'Which app is "Zhiliao"?'])
    ai = _RecAI("Which app is Zhiliao paid")
    r = await _svc(local, ai).translate(
        "「智聊」具体是哪个软件", target_lang="en", source_lang="zh")
    assert ai.calls == 0 and local.calls == 2
    assert r.ok and "Zhiliao" in r.translated_text

    deepl = _RecDeepL(outs=['the "智聊" app', 'the "Zhiliao" app'])
    pro = await _svc(deepl).translate(
        "「智聊」具体是哪个软件", target_lang="en", source_lang="zh", tier="pro")
    assert deepl.calls == 2 and "Zhiliao" in pro.translated_text

    blocked = _RecDeepL()
    std = await _svc(blocked).translate(
        "「智聊」具体是哪个软件", target_lang="en", source_lang="zh")
    assert blocked.calls == 0 and std.needs_human


# ── 泄漏 6：Ollama 冷却失败后不得落到云端 LLM ────────────────────────────────

@pytest.mark.asyncio
async def test_ollama_failure_does_not_fall_through_to_cloud_llm():
    mt = _CoolingMT()
    ai = _RecAI()
    r = await _svc(mt, ai).translate("hello friend", target_lang="zh", source_lang="en")
    assert mt.calls == 1 and ai.calls == 0
    assert r.ok is False and "timeout" in r.error and r.free_tier_blocked

    mt.calls = 0
    ai.calls = 0
    pro = await _svc(mt, ai).translate(
        "hello friend", target_lang="zh", source_lang="en", tier="pro")
    assert mt.calls == 1 and ai.calls == 1 and pro.ok and pro.provider == "ai"


@pytest.mark.asyncio
async def test_low_confidence_does_not_switch_std_onto_ai():
    local = _Local(text="我想你了")
    ai = _RecAI("君が恋しい")
    router = EngineRouter([local, ai], min_confidence=0.5)
    svc = TranslationService(engine_router=router, free_tier_zero_cost=True)
    r = await svc.translate("我想你了", target_lang="ja", source_lang="zh")
    assert ai.calls == 0 and local.calls >= 1
    assert r.ok and r.provider == "ollama_mt"


# ── 泄漏 7/8：对照选译与融合 bare_chat ──────────────────────────────────────

@pytest.mark.asyncio
async def test_compare_skips_paid_engines_on_std():
    local = _Local()
    ai = _RecAI()
    router = EngineRouter([local, ai])
    rows = await router.compare(
        "hi", source_lang="en", target_lang="zh", tier="", free_tier_enforce=True)
    by = {row.engine: row for row in rows}
    assert by["ollama_mt"].ok and by["ai"].error == "free_tier_paid_engine_blocked"
    assert ai.calls == 0
    rows_pro = await router.compare("hi", source_lang="en", target_lang="zh", tier="pro")
    assert {row.engine: row.ok for row in rows_pro}["ai"] is True
    assert ai.calls == 1


@pytest.mark.asyncio
async def test_fusion_bare_chat_blocked_on_std_and_runs_on_pro():
    client = _Chat("你好呀，明天见！")
    svc = TranslationService(engines=[AIEngine(client)], free_tier_zero_cost=True)
    cands = [
        {"engine": "m1", "ok": True, "translated_text": "你好，明天见", "confidence": 0.8},
        {"engine": "m2", "ok": True, "translated_text": "您好呀明天见面", "confidence": 0.7},
    ]
    blocked = await fuse_compare_candidates(
        svc, text="hi, see you tomorrow", candidates=cands,
        source_lang="en", target_lang="zh", tier="")
    assert blocked["ok"] is False and blocked["reason"] == "free_tier_paid_engine_blocked"
    assert client.calls == 0
    assert get_translation_engine_stats().dump()["paid_blocked"].get("fusion", 0) >= 1

    legacy = TranslationService(engines=[AIEngine(client)])
    observed = await fuse_compare_candidates(
        legacy, text="hi, see you tomorrow", candidates=cands,
        source_lang="en", target_lang="zh", tier="")
    assert observed["ok"] is True and client.calls >= 1
    assert get_translation_engine_stats().dump()["paid_would_block"].get("fusion", 0) >= 1

    ok = await fuse_compare_candidates(
        svc, text="hi, see you tomorrow", candidates=cands,
        source_lang="en", target_lang="zh", tier="pro")
    assert ok["ok"] is True and ok["text"] == "你好呀，明天见！"
    assert client.calls >= 1


class _LanCustom(OpenAICompatEngine):
    """局域网 OpenAI 兼容线仍然是付费类。"""

    @property
    def available(self):
        return True


@pytest.mark.asyncio
async def test_custom_openai_compat_is_paid_even_on_lan():
    custom = _LanCustom(
        "qwen_mt", base_url="http://127.0.0.1:9/v1", model="m", api_key="none")
    assert is_paid_engine(custom) is True
    calls = []

    async def _fake(text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        calls.append(text)
        return EngineResult("你好", custom.name, True)

    custom.translate = _fake
    r = await _svc(custom).translate("hello friend", target_lang="zh", source_lang="en")
    assert calls == [] and r.error == "free_tier_no_zero_cost_engine"
    pro = await _svc(custom).translate(
        "hello friend", target_lang="zh", source_lang="en", tier="pro")
    assert pro.ok and calls == ["hello friend"]


# ── 钱包降级、认证档、缓存残留 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wallet_degrade_to_std_then_blocks_paid(monkeypatch):
    monkeypatch.setattr(
        "src.licensing.token_ledger.should_degrade_action",
        lambda *a, **k: True,
    )
    ai = _RecAI()
    r = await _svc(ai).translate(
        "hello friend", target_lang="zh", source_lang="en", tier="pro")
    assert ai.calls == 0 and r.needs_human and r.error == "free_tier_no_zero_cost_engine"

    deepl = _RecDeepL()
    # 降级后不再偏好 DeepL（既有时序：先降级再偏好）。
    cert = await _svc(deepl).translate(
        "hello friend", target_lang="zh", source_lang="en", tier="certified")
    assert deepl.calls == 0 and cert.needs_human


@pytest.mark.asyncio
async def test_certified_still_prefers_deepl_when_not_degraded():
    deepl = _RecDeepL()
    ai = _RecAI()
    r = await _svc(ai, deepl).translate(
        "hello friend", target_lang="zh", source_lang="en", tier="certified")
    assert r.ok and r.provider == "deepl" and deepl.calls == 1 and ai.calls == 0


@pytest.mark.asyncio
async def test_cache_hit_does_not_place_a_new_paid_call():
    """缓存键不含 tier：命中旧译文是零新增调用，不是付费 API。"""
    ai = _RecAI()
    svc = _svc(ai)
    first = await svc.translate(
        "hello friend", target_lang="zh", source_lang="en", tier="pro")
    assert first.ok and ai.calls == 1
    second = await svc.translate(
        "hello friend", target_lang="zh", source_lang="en", tier="std")
    assert second.cached is True and second.translated_text == first.translated_text
    assert ai.calls == 1
