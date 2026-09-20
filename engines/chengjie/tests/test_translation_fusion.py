"""P1-XF/XT/XM（2026-08-16）门禁：智能融合 + 语气控制 + 矩阵实测统计。

融合不变量（改 translation_fusion 前先读模块 docstring）：
  候选 <2 / 全同质 / 无 LLM 通道 / 输出烂（空、错语种）/ 置信不过闸 → ok=False
  带原因（前端不出卡，对照功能零影响）；过闸 → text + engines_used + confidence。
语气不变量：chat 家族保留核心指令 + 语气附加；词汇表外 style 维持旧「faithful」；
  raw_mode 线无指令通道 → supports_tone=False；DeepL 走 prefer_* formality。
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ai.translation_engines import (
    AIEngine,
    DeepLEngine,
    EngineRouter,
    EngineResult,
    OpenAICompatEngine,
    build_chat_tone,
    build_engines,
    deepl_formality,
    style_tone_instruction,
)
from src.ai.translation_fusion import (
    build_fusion_prompt,
    fuse_compare_candidates,
    usable_candidates,
)
from src.ai.translation_service import TranslationService


# ── 融合纯函数 ───────────────────────────────────────────────────────────────

def _cand(engine, text, ok=True, conf=0.9):
    return {"engine": engine, "ok": ok, "translated_text": text, "confidence": conf}


def test_usable_candidates_dedupes_normalized_text():
    cands = [
        _cand("a", "你好  世界"),
        _cand("b", "你好 世界"),          # 空白差异 = 同质，去重
        _cand("c", "你好，世界"),          # 标点差异 = 真差异，保留
        _cand("d", "", ok=True),           # 空文本剔除
        _cand("e", "boom", ok=False),      # 失败候选剔除
    ]
    kept = usable_candidates(cands)
    assert [c["engine"] for c in kept] == ["a", "c"]


def test_fusion_prompt_carries_source_and_candidates():
    p = build_fusion_prompt("hello there", [_cand("m1", "你好呀"), _cand("m2", "您好")],
                            source_lang="en", target_lang="zh")
    assert "hello there" in p and "你好呀" in p and "您好" in p
    assert "[m1]" in p and "[m2]" in p
    assert "Chinese" in p                      # LANG_NAMES 展开目标语名
    assert "ONLY the final translation" in p   # 只输出终稿约定


class _FusionAI:
    """假 ai_client：generate_reply 干净通道返回预设融合稿并记录 prompt。

    必须带 chat 方法——AIEngine.available 以 hasattr(ai,'chat') 为存活判据
    （真实 AIClient 契约），缺了会被判不可用。
    """

    def __init__(self, reply):
        self._reply = reply
        self.prompts = []

    async def generate_reply(self, prompt, *, context=None, conversation_history=None,
                             _skip_quality_check=False):
        self.prompts.append((prompt, context))
        return self._reply

    async def chat(self, prompt, ctx=None):
        return self._reply


def _svc_with_ai(reply):
    return TranslationService(engines=[AIEngine(_FusionAI(reply))])


@pytest.mark.asyncio
async def test_fuse_happy_path_returns_fused_text():
    svc = _svc_with_ai("你好呀，明天见！")
    cands = [_cand("m1", "你好，明天见", conf=0.8), _cand("m2", "您好呀明天见面", conf=0.7)]
    out = await fuse_compare_candidates(svc, text="hi, see you tomorrow",
                                        candidates=cands, source_lang="en",
                                        target_lang="zh")
    assert out["ok"] and out["text"] == "你好呀，明天见！"
    assert out["engine"] == "fusion" and out["engines_used"] == ["m1", "m2"]
    assert out["confidence"] > 0 and out["confidence_tier"] in ("high", "mid", "low")


@pytest.mark.asyncio
async def test_fuse_skips_not_enough_and_unanimous():
    svc = _svc_with_ai("whatever")
    one = await fuse_compare_candidates(svc, text="hi", candidates=[_cand("m1", "你好")],
                                        source_lang="en", target_lang="zh")
    assert not one["ok"] and one["reason"] == "not_enough_candidates"
    same = await fuse_compare_candidates(
        svc, text="hi", candidates=[_cand("m1", "你好"), _cand("m2", "你好")],
        source_lang="en", target_lang="zh")
    assert not same["ok"] and same["reason"] == "unanimous"   # 各线一致=好消息，不融合


@pytest.mark.asyncio
async def test_fuse_requires_llm_channel():
    svc = TranslationService(engines=[])   # 无 ai 引擎
    out = await fuse_compare_candidates(
        svc, text="hi", candidates=[_cand("m1", "你好"), _cand("m2", "您好")],
        source_lang="en", target_lang="zh")
    assert not out["ok"] and out["reason"] == "no_llm"


@pytest.mark.asyncio
async def test_fuse_rejects_bad_output_and_below_best():
    # 输出错语种（目标 zh 回英文）→ bad_output，绝不把烂稿端给坐席
    svc = _svc_with_ai("this is english not chinese at all")
    cands = [_cand("m1", "你好，明天见", conf=0.9), _cand("m2", "您好呀", conf=0.8)]
    out = await fuse_compare_candidates(svc, text="hi, see you tomorrow",
                                        candidates=cands, source_lang="en",
                                        target_lang="zh")
    assert not out["ok"] and out["reason"] == "bad_output"
    # LLM 通道抛异常 → fusion_error（fail-open，不上抛）
    class _Boom:
        async def generate_reply(self, *a, **k):
            raise RuntimeError("down")

        async def chat(self, *a, **k):
            raise RuntimeError("down")
    svc2 = TranslationService(engines=[AIEngine(_Boom())])
    out2 = await fuse_compare_candidates(svc2, text="hi", candidates=cands,
                                         source_lang="en", target_lang="zh")
    assert not out2["ok"] and out2["reason"] == "fusion_error"


# ── 语气控制 ─────────────────────────────────────────────────────────────────

def test_tone_instruction_vocabulary():
    assert style_tone_instruction("chat") == ""
    assert "professional" in style_tone_instruction("formal")
    assert "friendly" in style_tone_instruction("friendly")
    assert "sales" in style_tone_instruction("sales")
    assert style_tone_instruction("nosuch") == ""


def test_build_chat_tone_families():
    chat = build_chat_tone("chat")
    assert "Keep the meaning" in chat and "sales" not in chat
    sales = build_chat_tone("sales")
    assert "Keep the meaning" in sales and "sales tone" in sales   # 核心线保留+语气附加
    legacy = build_chat_tone("faithful")                            # 词汇表外=旧语义
    assert legacy == "Translate faithfully. Do not add explanations."


@pytest.mark.asyncio
async def test_ai_engine_prompt_carries_tone():
    ai = _FusionAI("你好")
    eng = AIEngine(ai)
    await eng.translate("hello", source_lang="en", target_lang="zh", style="sales")
    prompt = ai.prompts[0][0]
    assert "sales tone" in prompt and "hello" in prompt


def test_openai_compat_tone_and_raw_mode_capability():
    tone_line = OpenAICompatEngine("x", base_url="https://a", model="m", api_key="k")
    p = tone_line.build_payload("hi", "en", "zh", style="formal")
    assert "professional" in p["messages"][1]["content"]
    assert tone_line.supports_tone is True
    raw = OpenAICompatEngine("q", base_url="https://a", model="m", api_key="k",
                             raw_mode=True)
    assert raw.supports_tone is False   # 原文直送无指令通道，语气不生效
    pr = raw.build_payload("hi", "en", "zh", style="formal")
    assert pr["messages"] == [{"role": "user", "content": "hi"}]


def test_deepl_formality_mapping():
    assert deepl_formality("formal") == "prefer_more"
    assert deepl_formality("friendly") == "prefer_less"
    assert deepl_formality("sales") == "prefer_less"
    assert deepl_formality("chat") == "" and deepl_formality("") == ""
    assert DeepLEngine.supports_tone is True


def test_describe_rows_carry_tone_capability():
    cfg = {"engines": {
        "order": ["youdao", "deepl", "ai"],
        "youdao": {"app_key": "a", "app_secret": "s"},
        "deepl": {"api_key": "k"},
    }}
    rows = EngineRouter(build_engines(cfg, ai_client=None)).describe("zh")["engines"]
    by = {r["engine"]: r["tone"] for r in rows}
    assert by["ai"] is True and by["deepl"] is True
    assert by["youdao"] is False   # NMT 线天然忽略语气，能力位如实为 False


# ── 矩阵实测统计 join ────────────────────────────────────────────────────────

def test_engine_matrix_joins_runtime_stats():
    from src.ai.translation_engine_stats import get_translation_engine_stats

    stats = get_translation_engine_stats()
    stats.reset()
    try:
        stats.record("ai", ok=True, latency_ms=120)
        stats.record("ai", ok=True, latency_ms=80)
        svc = _svc_with_ai("x")
        rows = svc.engine_matrix("zh")["engines"]
        ai_row = next(r for r in rows if r["engine"] == "ai")
        assert ai_row["avg_ms"] == 100.0 and ai_row["ok_rate"] == 1.0
    finally:
        stats.reset()


def test_engine_matrix_silent_without_traffic():
    from src.ai.translation_engine_stats import get_translation_engine_stats

    get_translation_engine_stats().reset()
    svc = _svc_with_ai("x")
    rows = svc.engine_matrix("zh")["engines"]
    assert all("avg_ms" not in r for r in rows)   # 零流量不带键，前端不渲染


# ── compare 路由 fuse 端到端 ─────────────────────────────────────────────────

class _ZhEngineA:
    name = "ai"
    label = "AI"
    available = True

    def __init__(self, client):
        self._impl = AIEngine(client)

    def supports_target(self, t):
        return True

    async def bare_chat(self, prompt):
        return await self._impl.bare_chat(prompt)

    async def translate(self, text, *, source_lang, target_lang, style="chat",
                        glossary_hint=""):
        return EngineResult("你好，明天见", self.name, True)


class _ZhEngineB:
    name = "m2"
    label = "M2"
    available = True

    def supports_target(self, t):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat",
                        glossary_hint=""):
        return EngineResult("您好呀，明天见面", self.name, True)


def _compare_app():
    from src.web.routes.unified_inbox_translate_routes import register_translate_routes

    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_translate_routes(app, api_auth=api_auth)
    app.state.translation_service = TranslationService(
        engines=[_ZhEngineA(_FusionAI("你好呀，明天见！")), _ZhEngineB()])
    return app


def test_compare_route_fuse_end_to_end():
    client = TestClient(_compare_app())
    r = client.post("/api/unified-inbox/translate-compare", json={
        "text": "hello, see you tomorrow", "target_lang": "zh", "fuse": True,
    })
    body = r.json()
    assert body["ok"] is True
    fusion = body["compare"]["fusion"]
    assert fusion["ok"] and fusion["text"] == "你好呀，明天见！"
    assert set(fusion["engines_used"]) == {"ai", "m2"}


def test_compare_route_without_fuse_has_no_fusion_key():
    client = TestClient(_compare_app())
    r = client.post("/api/unified-inbox/translate-compare", json={
        "text": "hello, see you tomorrow", "target_lang": "zh",
    })
    assert "fusion" not in r.json()["compare"]
