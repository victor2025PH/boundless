"""platform/enable 契约 · /api/translate + /api/enable/status 路由测试。

仿 test_care_routes.py 模式：裸 FastAPI() + register_enable_routes + TestClient。
重点覆盖契约的关键退化路径：无翻译 provider/key 时必须优雅返回
{"available": false, "error": "provider_unavailable"}，而不是 500；
以及用假 ai_client（零网络/零 key）覆盖 ok=True 的正常译文分支。
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.web.routes.enable_routes import register_enable_routes


class _FakeAI:
    """零网络依赖的假 AI client：只需有 async chat()，即可让 AIEngine.available=True。"""

    async def chat(self, prompt, context=None):
        return "你好朋友"


def _client():
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_enable_routes(app, api_auth=_auth, config_manager=None)
    return TestClient(app), app


# ── ① 正常调用：译文结构 + provider_unavailable 优雅退化 ──────────────────

def test_translate_without_provider_returns_available_false_not_500():
    """无任何翻译 provider/key 时的关键路径：ok=False → HTTP 200 + available:false，绝不 500。"""
    client, _app = _client()
    r = client.post("/api/translate", json={"text": "hello friend", "to_lang": "zh"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["error"] == "provider_unavailable"


def test_translate_with_available_engine_returns_translated_text():
    """注入假 ai_client（免真实 key/网络）覆盖 ok=True 正常译文分支。"""
    client, app = _client()
    app.state.ai_client = _FakeAI()
    text = "hello friend"
    r = client.post("/api/translate", json={"text": text, "to_lang": "zh"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "你好朋友"
    assert body["detected_lang"] == "en"
    assert body["chars"] == len(text)
    # 契约：available 由瘦客户端 setdefault 填入，引擎成功响应不必自带该字段
    assert "available" not in body


def test_translate_identity_when_source_equals_target():
    """source==target 时 TranslationService 走 identity 分支，ok=True 原样返回。"""
    client, _app = _client()
    r = client.post("/api/translate", json={"text": "你好", "to_lang": "zh", "from_lang": "zh"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "你好"
    assert body["detected_lang"] == "zh"


# ── ② status 端点可用 ──────────────────────────────────────────────────

def test_status_reports_not_ready_without_provider():
    client, _app = _client()
    r = client.get("/api/enable/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["translate_ready"] is False


def test_status_reports_ready_with_available_engine():
    client, app = _client()
    app.state.ai_client = _FakeAI()  # 必须在首次访问 translation_service 前设置（惰性单例）
    r = client.get("/api/enable/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["translate_ready"] is True


# ── ③ 缺参数 → 400/合理错误而非 500 ─────────────────────────────────────

def test_translate_missing_text_returns_400_not_500():
    client, _app = _client()
    r = client.post("/api/translate", json={"to_lang": "zh"})
    assert r.status_code == 400
    assert r.status_code != 500


def test_translate_blank_text_returns_400():
    client, _app = _client()
    r = client.post("/api/translate", json={"text": "   ", "to_lang": "zh"})
    assert r.status_code == 400


def test_translate_missing_to_lang_returns_400_not_500():
    client, _app = _client()
    r = client.post("/api/translate", json={"text": "hello"})
    assert r.status_code == 400
    assert r.status_code != 500


def test_translate_bad_json_body_returns_400_not_500():
    """畸形 body（非 JSON）：_body() 兜底吞掉解析异常，落到「缺 text」的 400 分支。"""
    client, _app = _client()
    r = client.post(
        "/api/translate", data="not-json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 400
    assert r.status_code != 500


# ── 额外：Depends(api_auth) 确实生效（未认证应被拒绝，不是形同虚设）──────────

def test_translate_enforces_api_auth_dependency():
    app = FastAPI()

    def _deny(request: Request):
        raise HTTPException(status_code=401, detail="unauthorized")

    register_enable_routes(app, api_auth=_deny, config_manager=None)
    client = TestClient(app)
    r = client.post("/api/translate", json={"text": "hi", "to_lang": "zh"})
    assert r.status_code == 401


def test_status_enforces_api_auth_dependency():
    app = FastAPI()

    def _deny(request: Request):
        raise HTTPException(status_code=401, detail="unauthorized")

    register_enable_routes(app, api_auth=_deny, config_manager=None)
    client = TestClient(app)
    r = client.get("/api/enable/status")
    assert r.status_code == 401
