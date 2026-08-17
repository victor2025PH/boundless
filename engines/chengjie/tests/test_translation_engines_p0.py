"""P0-XL（2026-08-16）翻译引擎矩阵扩容门禁。

覆盖三块：
  1. 通用 OpenAI 兼容线 ``OpenAICompatEngine``——一段配置=一条线路（raw_mode /
     payload_extra 占位符 / supports 白名单 / 语言完整性护栏 / 缺 key 隐藏）；
  2. Microsoft / Youdao / Baidu 三条专有 REST 线——语种映射让位语义 + 签名纯函数
     （有道 v3 input 截断契约 / 百度 MD5）+ 缺 key 不可用；
  3. ``build_engines`` 扩容解析（order 内置名 + custom 名 + 未知名忽略）、
     describe() 行携带 label、``/api/unified-inbox/translate`` 的 back 反译参数
     （P0-XL3：反译目标=正向探测源语；identity/未知源语不反译；不影响正向结果）。
"""
import hashlib

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ai.translation_engines import (
    BaiduEngine,
    EngineResult,
    EngineRouter,
    MicrosoftEngine,
    OpenAICompatEngine,
    YoudaoEngine,
    build_engines,
    youdao_sign_input,
)
from src.ai.translation_service import TranslationService


# ── 1. OpenAICompatEngine ────────────────────────────────────────────────────

def test_openai_compat_available_requires_base_model_key():
    assert OpenAICompatEngine("x", base_url="https://a", model="m", api_key="k").available
    assert not OpenAICompatEngine("x", base_url="", model="m", api_key="k").available
    assert not OpenAICompatEngine("x", base_url="https://a", model="", api_key="k").available
    # 缺 key = 该线路隐藏（云端线缺鉴权只会 401 白跑；LAN 端点填任意非空值即可）
    assert not OpenAICompatEngine("x", base_url="https://a", model="m", api_key="").available


def test_openai_compat_supports_whitelist_and_default_all():
    any_lang = OpenAICompatEngine("x", base_url="https://a", model="m", api_key="k")
    assert any_lang.supports_target("zh") and any_lang.supports_target("sw")
    scoped = OpenAICompatEngine("x", base_url="https://a", model="m", api_key="k",
                                supports=["zh", "en", "JA"])
    assert scoped.supports_target("zh") and scoped.supports_target("ja")
    assert scoped.supports_target("zh-CN")   # 基码归一
    assert not scoped.supports_target("sw")


def test_openai_compat_build_payload_instructive_mode():
    eng = OpenAICompatEngine("x", base_url="https://a", model="m", api_key="k",
                             temperature=0.3, max_tokens=512)
    p = eng.build_payload("你好", "zh", "en")
    assert p["model"] == "m" and p["temperature"] == 0.3 and p["max_tokens"] == 512
    assert p["messages"][0]["role"] == "system"
    assert "machine translation engine" in p["messages"][0]["content"]
    user = p["messages"][1]["content"]
    assert "你好" in user and "English" in user   # LANG_NAMES 展开目标语名


def test_openai_compat_build_payload_raw_mode_and_placeholders():
    eng = OpenAICompatEngine(
        "qwen_mt", base_url="https://a", model="qwen-mt-turbo", api_key="k",
        raw_mode=True,
        payload_extra={"translation_options": {
            "source_lang": "auto", "target_lang": "{target_name}",
            "codes": ["{source_lang}", "{target_lang}"],
        }},
    )
    p = eng.build_payload("你好啊", "zh", "en")
    # raw_mode：原文直送，无 system 指令（Qwen-MT 契约）
    assert p["messages"] == [{"role": "user", "content": "你好啊"}]
    opts = p["translation_options"]
    assert opts["target_lang"] == "English"          # {target_name} 展开
    assert opts["codes"] == ["zh", "en"]             # 列表内字符串同样替换
    assert opts["source_lang"] == "auto"             # 无占位符原样保留


@pytest.mark.asyncio
async def test_openai_compat_translate_success_and_lang_guard():
    eng = OpenAICompatEngine("x", base_url="https://a", model="m", api_key="k")

    async def _fake_post_ok(payload):
        return {"choices": [{"message": {"content": "Hello there"}}]}

    eng._post = _fake_post_ok
    res = await eng.translate("你好", source_lang="zh", target_lang="en")
    assert res.ok and res.text == "Hello there" and res.engine == "x"

    # 语言完整性护栏：目标 en 却回中文（identity 回显/答非所译）→ 判失败让位下一引擎
    async def _fake_post_cjk(payload):
        return {"choices": [{"message": {"content": "你好你好你好"}}]}

    eng._post = _fake_post_cjk
    res2 = await eng.translate("你好", source_lang="zh", target_lang="en")
    assert not res2.ok and res2.error == "target_lang_mismatch"


@pytest.mark.asyncio
async def test_openai_compat_translate_error_paths():
    eng = OpenAICompatEngine("x", base_url="https://a", model="m", api_key="k",
                             supports=["en"])
    assert (await eng.translate("hi", source_lang="en", target_lang="sw")).error \
        == "unsupported_target:sw"
    assert (await eng.translate("  ", source_lang="zh", target_lang="en")).error \
        == "empty_input"

    async def _boom(payload):
        raise RuntimeError("down")

    eng._post = _boom
    res = await eng.translate("hi", source_lang="zh", target_lang="en")
    assert not res.ok and "RuntimeError" in res.error


# ── 1b. OllamaMTEngine 的 OpenAI 兼容模式（P2-XL：vLLM 官方精度部署用） ──────

@pytest.mark.asyncio
async def test_ollama_mt_openai_api_mode_payload_and_parse():
    from src.ai.translation_engines import OllamaMTEngine

    eng = OllamaMTEngine(base_url="http://vllm:8002/v1", model="hymt15",
                         api_key="vllm", temperature=0.0, api="openai")
    seen = {}

    async def _fake(url, payload):
        seen["payload"] = payload
        return {"choices": [{"message": {"content": "你好，世界"}}]}

    eng._post_chat = _fake
    res = await eng.translate("hello world", source_lang="en", target_lang="zh")
    assert res.ok and res.text == "你好，世界"
    p = seen["payload"]
    # OpenAI 形状：顶层 max_tokens/temperature；绝无 Ollama 专有的 options/keep_alive
    assert p["max_tokens"] == 1024 and p["temperature"] == 0.0
    assert "options" not in p and "keep_alive" not in p and "stream" not in p
    # prompt 仍是 Hunyuan 官方模板（zh 目标走中文指令）
    assert "翻译成中文" in p["messages"][0]["content"]


def test_ollama_mt_openai_url_normalization():
    from src.ai.translation_engines import OllamaMTEngine

    assert OllamaMTEngine._openai_chat_url("http://h:8002") \
        == "http://h:8002/v1/chat/completions"
    assert OllamaMTEngine._openai_chat_url("http://h:8002/v1") \
        == "http://h:8002/v1/chat/completions"
    assert OllamaMTEngine._openai_chat_url("http://h:8002/v1/") \
        == "http://h:8002/v1/chat/completions"


def test_build_engines_ollama_mt_api_passthrough():
    cfg = {"engines": {"order": ["ollama_mt"], "ollama_mt": {
        "base_url": "http://h:8002/v1", "model": "hymt15", "api": "openai"}}}
    eng = build_engines(cfg, ai_client=None)[0]
    assert eng.name == "ollama_mt" and eng._api == "openai"
    # 缺省不传 api=native 旧行为
    cfg2 = {"engines": {"order": ["ollama_mt"], "ollama_mt": {
        "base_url": "http://h:11434", "model": "hy"}}}
    assert build_engines(cfg2, ai_client=None)[0]._api == "native"


# ── 2. Microsoft / Youdao / Baidu ────────────────────────────────────────────

def test_rest_engines_availability_gating():
    assert not MicrosoftEngine("").available
    assert MicrosoftEngine("k").available
    assert not YoudaoEngine("", "").available
    assert not YoudaoEngine("app", "").available
    assert YoudaoEngine("app", "sec").available
    assert not BaiduEngine("", "").available
    assert BaiduEngine("id", "sec").available


def test_rest_engines_lang_mapping_and_yield():
    ms, yd, bd = MicrosoftEngine("k"), YoudaoEngine("a", "s"), BaiduEngine("i", "s")
    for eng in (ms, yd, bd):
        assert eng.supports_target("zh") and eng.supports_target("en")
        assert not eng.supports_target("sw")   # 集外让位下一引擎，不硬吃
    # 各家私有码正确映射（错码=100% 请求失败，必须钉住）
    assert bd.build_form("hi", "en", "ja")["to"] == "jp"
    assert bd.build_form("hi", "en", "ko")["to"] == "kor"
    assert bd.build_form("hi", "en", "vi")["to"] == "vie"
    assert yd.build_form("hi", "en", "zh")["to"] == "zh-CHS"


def test_youdao_sign_input_truncation_contract():
    assert youdao_sign_input("short") == "short"
    assert youdao_sign_input("a" * 20) == "a" * 20
    long = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"   # 36 字符
    assert youdao_sign_input(long) == long[:10] + "36" + long[-10:]


def test_youdao_form_sign_recomputes():
    yd = YoudaoEngine("appk", "secret")
    form = yd.build_form("hello world, this is a long text over twenty chars", "en", "zh")
    raw = "appk" + youdao_sign_input(form["q"]) + form["salt"] + form["curtime"] + "secret"
    assert form["sign"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert form["signType"] == "v3"


def test_baidu_form_sign_recomputes():
    bd = BaiduEngine("appid", "key")
    form = bd.build_form("你好", "zh", "en")
    raw = "appid" + "你好" + form["salt"] + "key"
    assert form["sign"] == hashlib.md5(raw.encode("utf-8")).hexdigest()
    assert form["from"] == "zh" and form["to"] == "en"


# ── 3. build_engines / describe / back 反译 ─────────────────────────────────

def test_build_engines_parses_new_names_and_custom_lines():
    cfg = {"engines": {
        "order": ["ollama_mt", "gemini_flash", "youdao", "baidu", "microsoft",
                  "nosuch_engine", "ai"],
        "ollama_mt": {"base_url": "http://gpu:11434", "model": "hy"},
        "youdao": {"app_key": "a", "app_secret": "s"},
        "baidu": {"app_id": "i", "secret": "s"},
        "microsoft": {"api_key": "k", "region": "eastasia"},
        "custom": {"gemini_flash": {
            "label": "Gemini Flash",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-2.5-flash", "api_key": "g",
        }},
    }}
    engines = build_engines(cfg, ai_client=None)
    names = [e.name for e in engines]
    # 顺序即故障转移优先级；未知名（无 custom 条目）忽略不炸装配
    assert names == ["ollama_mt", "gemini_flash", "youdao", "baidu", "microsoft", "ai"]
    gem = engines[1]
    assert isinstance(gem, OpenAICompatEngine) and gem.label == "Gemini Flash"
    assert gem.available


def test_describe_rows_carry_labels():
    cfg = {"engines": {
        "order": ["youdao", "ai"],
        "youdao": {"app_key": "a", "app_secret": "s"},
    }}
    router = EngineRouter(build_engines(cfg, ai_client=None))
    rows = router.describe("zh")["engines"]
    by = {r["engine"]: r for r in rows}
    assert by["youdao"]["label"] == "Youdao"
    assert by["ai"]["label"] == "AI"


class _BackEchoEngine:
    """按目标语分流的桩引擎：zh 目标回中文、其余回英文（让 detect 走真实路径）。"""

    name = "ai"
    label = "AI"
    available = True

    def supports_target(self, target_lang):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat",
                        glossary_hint=""):
        out = "你好，明天见" if target_lang == "zh" else "hello, see you tomorrow"
        return EngineResult(out, self.name, True)


def _app():
    from src.web.routes.unified_inbox_translate_routes import register_translate_routes

    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_translate_routes(app, api_auth=api_auth)
    app.state.translation_service = TranslationService(engines=[_BackEchoEngine()])
    return app


def test_translate_back_param_round_trips():
    client = TestClient(_app())
    r = client.post("/api/unified-inbox/translate", json={
        "text": "hello there my friend", "target_lang": "zh", "back": True,
    })
    body = r.json()
    assert body["ok"] and body["translation"]["translated_text"] == "你好，明天见"
    back = body["back"]
    # 反译目标 = 正向探测出的源语言（en），产物是引擎的 en 输出
    assert back["ok"] and back["target_lang"] == "en"
    assert back["text"] == "hello, see you tomorrow"


def test_translate_back_absent_when_not_requested():
    client = TestClient(_app())
    r = client.post("/api/unified-inbox/translate", json={
        "text": "hello there my friend", "target_lang": "zh",
    })
    assert "back" not in r.json()


def test_translate_back_skips_identity_no_source():
    # 源语==目标语 → 正向 identity；反译无独立源语可回 → ok=False 带原因（不瞎译）
    client = TestClient(_app())
    r = client.post("/api/unified-inbox/translate", json={
        "text": "你好呀朋友", "target_lang": "zh", "back": True,
    })
    body = r.json()
    assert body["ok"] and body["translation"]["provider"] == "identity"
    assert body["back"]["ok"] is False
    assert body["back"]["error"] == "no_source_lang"
