# -*- coding: utf-8 -*-
"""AI 助手悬浮球路由门禁（stub 鉴权 / 假 LLM / bug_intake@tmp，零生产依赖）。

覆盖：enabled 闸 / bootstrap 探针 / 问答流（命中→LLM 引用回答；零命中→诚实
不知道且**不调 LLM**）/ 限频 429 / 报障写 bug_tickets（platform 盖章 + webuser
chat_id + 附件 magic bytes 消毒 + 去重并单）/ 我的工单（面板即回访盖 notify_ts
+ 只看自己的）/ 反馈 / health / agent 白名单静态钉 / TG 回访冲刷排除 webuser。
"""
from __future__ import annotations

import base64
import json
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.web.routes.assistant_routes import (
    assistant_llm_extra_body,
    register_assistant_routes,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class _FakeAI:
    def __init__(self, answer="按 [S1] 操作即可。"):
        self.answer = answer
        self.calls = 0

    async def generate_reply(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return self.answer


class _FakeSM:
    def __init__(self, ai):
        self.ai_client = ai


def _mk_app(monkeypatch, tmp_path, *, enabled=True, rate_per_min=10,
            fake_ai=None, voice=False, audio_pipeline=True, vision=False,
            query_llm=None):
    # 数据根隔离（bug_intake/help_kb/qa_log 全走 config_dir 契约）
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    data_root = tmp_path / "data"
    (data_root / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AITR_DATA_DIR", str(data_root))

    from src.ops import bug_intake

    bug_intake.reset_state_for_tests()

    # 进程级单例 → 每测试新实例
    from src.assistant import help_kb as hk
    from src.assistant import qa_log as ql
    from src.assistant import rate_limit as rl
    from src.assistant import stats as st

    kb = hk.HelpKB(data_root / "config" / "assistant_help.db")
    kb.upsert_entries([
        {"id": "t1", "title": "语音克隆发送", "title_en": "Voice clone send",
         "content": "在收件箱右栏生成并发送克隆语音",
         "content_en": "Generate and send from copilot panel",
         "keywords": "语音 voice 发语音 克隆", "path": "/workspace",
         "anchor": "#t1-anchor"},
    ])
    log = ql.AssistantQALog(data_root / "config" / "assistant.db")
    limiter = rl.AssistantRateLimiter()
    stats = st.AssistantStats()
    monkeypatch.setattr(hk, "get_help_kb", lambda: kb)
    monkeypatch.setattr(ql, "get_qa_log", lambda: log)
    monkeypatch.setattr(rl, "get_rate_limiter", lambda: limiter)
    monkeypatch.setattr(st, "get_assistant_stats", lambda: stats)

    ai = fake_ai or _FakeAI()
    import src.web.web_context as wc

    monkeypatch.setattr(wc, "resolve_skill_manager",
                        lambda tg, app: _FakeSM(ai))

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")

    @app.get("/_test/login/{role}")
    async def _login(request: Request, role: str):
        request.session["role"] = role
        request.session["username"] = "t-" + role
        request.session["user_id"] = "u-" + role
        return {"ok": True}

    cfg = {
        "assistant": {
            "enabled": enabled,
            "query": {"rate_per_min": rate_per_min, "rate_per_day": 100,
                      "top_k": 3, "min_score": 0.5,
                      **({"llm": query_llm} if query_llm else {})},
            "report": {"enabled": True, "max_shot_kb": 64},
            "voice": {"enabled": voice},
            "vision": {"enabled": vision},
        },
        "web_admin": {"site_name": "测试站"},
    }
    if audio_pipeline:
        cfg["audio_pipeline"] = {"enabled": True, "backend": "openai",
                                 "base_url": "http://x/v1"}
    ctx = types.SimpleNamespace(
        api_auth=lambda r: None,
        config_manager=types.SimpleNamespace(config=cfg),
        telegram_client=None,
    )
    register_assistant_routes(app, ctx)
    return app, ai, log, kb, stats


def _client(app, role="agent"):
    c = TestClient(app)
    c.get(f"/_test/login/{role}")
    return c


def _stream_events(resp) -> list[dict]:
    """query 事件数组。协议史：ndjson 流式（被压缩层掐断，8/20 弃）→ JSON
    {ok,events}（8/20-8/21）→ **SSE text/event-stream**（8/21 P2 真流式，
    压缩层白名单豁免）。本助手三代通吃：JSON 整包 / SSE `data:` 帧 / 裸行。"""
    body = resp.text.strip()
    if body.startswith("{") and '"events"' in body:
        j = resp.json()
        assert j.get("ok") is True
        return list(j.get("events") or [])
    out = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ────────────────────────────────────────────────────────── enabled 闸
def test_bootstrap_disabled(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path, enabled=False)
    c = _client(app)
    r = c.get("/api/assistant/bootstrap")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "enabled": False}
    assert c.post("/api/assistant/query", json={"q": "x"}).status_code == 403


def test_bootstrap_enabled_shape(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    j = c.get("/api/assistant/bootstrap").json()
    assert j["enabled"] and j["kb_entries"] == 1
    assert j["brand"] == "测试站"
    assert j["report_enabled"] is True
    assert isinstance(j["chips"], list)


# ────────────────────────────────────────────────────────── query
def test_query_hit_streams_answer_and_logs(monkeypatch, tmp_path):
    app, ai, log, _kb, stats = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    r = c.post("/api/assistant/query",
               json={"q": "怎么发语音", "page": "/workspace", "lang": "zh"})
    assert r.status_code == 200
    evs = _stream_events(r)
    kinds = [e["ev"] for e in evs]
    assert kinds == ["meta", "delta", "done"]
    assert evs[0]["sources"] and evs[0]["sources"][0]["id"] == "t1"
    assert "[S1]" in evs[1]["text"]
    assert evs[2]["answered"] is True and evs[2]["qa_id"] > 0
    assert ai.calls == 1
    # prompt 带上下文与红线
    prompt = ai.last_kwargs["user_message"]
    assert "禁止编造功能" in prompt and "参考条目" in prompt
    assert log.stats(days=1)["answered"] == 1
    assert stats.dump()["answered"] == 1


def test_query_no_hit_honest_and_no_llm_call(monkeypatch, tmp_path):
    app, ai, log, _kb, stats = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    r = c.post("/api/assistant/query",
               json={"q": "qqxyzzy foobar", "page": "/workspace"})
    evs = _stream_events(r)
    assert [e["ev"] for e in evs] == ["meta", "delta", "done"]
    assert evs[0]["sources"] == []
    assert evs[2]["answered"] is False
    assert ai.calls == 0  # 零命中绝不调 LLM 裸编
    assert log.miss_list(days=1)
    assert stats.dump()["miss"] == 1


def test_query_rate_limited_429(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path, rate_per_min=1)
    c = _client(app)
    assert c.post("/api/assistant/query", json={"q": "怎么发语音"}).status_code == 200
    assert c.post("/api/assistant/query", json={"q": "怎么发语音"}).status_code == 429


def test_query_validation(monkeypatch, tmp_path):
    """空问题仍 400；长问题**不再拒绝**（2026-08-21 老板拍板取消字数限制，
    仅 20000 字静默截断防病态载荷）。"""
    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    assert c.post("/api/assistant/query", json={"q": ""}).status_code == 400
    r = c.post("/api/assistant/query", json={"q": "x" * 501})
    assert r.status_code == 200  # 长问题正常走链（此例零命中=诚实不知道）
    assert _stream_events(r)


def test_assistant_llm_extra_body_disables_v4_thinking():
    """与 AIClient 同口径：仅 deepseek-v4 + deepseek 端点默认关思维链。"""
    assert assistant_llm_extra_body(
        "https://api.deepseek.com/v1", "deepseek-v4-flash"
    ) == {"thinking": {"type": "disabled"}}
    assert assistant_llm_extra_body(
        "https://api.deepseek.com/v1", "deepseek-v4-flash", reasoning=True
    ) == {}
    assert assistant_llm_extra_body(
        "https://api.openai.com/v1", "deepseek-v4-flash"
    ) == {}
    assert assistant_llm_extra_body(
        "https://api.deepseek.com/v1", "deepseek-chat"
    ) == {}


def test_query_direct_llm_falls_back_to_main_chain(monkeypatch, tmp_path):
    """assistant.query.llm 配置了直连端点但端点不可达 → 自动回落
    generate_reply 主链容灾（直连是快路径不是单点）。"""
    app, ai, *_ = _mk_app(
        monkeypatch, tmp_path,
        query_llm={"base_url": "http://127.0.0.1:1/v1", "model": "x",
                   "api_key": "k"})
    c = _client(app)
    r = c.post("/api/assistant/query",
               json={"q": "怎么发语音", "page": "/workspace"})
    assert r.status_code == 200
    evs = _stream_events(r)
    assert [e["ev"] for e in evs] == ["meta", "delta", "done"]
    assert evs[2]["answered"] is True
    assert ai.calls == 1  # 直连失败后主链兜底真的被调了


def test_query_direct_llm_empty_reasoning_stream_falls_back(monkeypatch, tmp_path):
    """直连流只吐 reasoning_content、content 为空 → 视为失败并回落主链。

    2026-08-23 线上：deepseek-v4-flash 默认开思维链，助手直连没关，
    流在 meta 后结束且零 delta → 前端误报「网络异常」。
    """
    class _Delta:
        def __init__(self, content=None):
            self.content = content

    class _Chunk:
        def __init__(self, content=None):
            self.choices = [types.SimpleNamespace(delta=_Delta(content))]

    class _Stream:
        def __init__(self):
            self._items = [_Chunk(None), _Chunk("")]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    class _FakeClient:
        last_kw = None

        def __init__(self, **kwargs):
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self._create))

        async def _create(self, **kwargs):
            _FakeClient.last_kw = kwargs
            return _Stream()

        async def close(self):
            return None

    import openai as _openai
    monkeypatch.setattr(_openai, "AsyncOpenAI", _FakeClient)

    app, ai, *_ = _mk_app(
        monkeypatch, tmp_path,
        query_llm={"base_url": "https://api.deepseek.com/v1",
                   "model": "deepseek-v4-flash", "api_key": "k"})
    c = _client(app)
    r = c.post("/api/assistant/query",
               json={"q": "怎么发语音", "page": "/workspace"})
    assert r.status_code == 200
    evs = _stream_events(r)
    assert [e["ev"] for e in evs] == ["meta", "delta", "done"]
    assert evs[2]["answered"] is True
    assert ai.calls == 1
    assert _FakeClient.last_kw["extra_body"] == {
        "thinking": {"type": "disabled"}}


def test_query_report_hint_flag(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    r = c.post("/api/assistant/query", json={"q": "语音按钮点了没反应"})
    evs = _stream_events(r)
    assert evs[0]["report_hint"] is True


# ────────────────────────────────────────────────────────── report
def test_report_creates_ticket_with_platform_stamp(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    shot = "data:image/png;base64," + base64.b64encode(_PNG).decode()
    r = c.post("/api/assistant/report",
               json={"desc": "语音发送按钮点了没反应", "page": "/workspace",
                     "shot_b64": shot, "ui_build": "20260819"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["ticket_id"] > 0 and j["dup"] is False

    from src.ops.bug_intake import get_ticket

    t = get_ticket(j["ticket_id"])
    assert t["platform"] == "assistant"
    assert t["chat_id"] == "webuser:u-agent"
    assert "[env]" in t["body"] and "/workspace" in t["body"]
    # 附件真实落盘且 magic bytes 是 PNG
    from src.licensing.data_paths import config_dir

    shots = list((config_dir() / "assistant_reports" / str(j["ticket_id"])
                  ).glob("shot_*.png"))
    assert len(shots) == 1
    assert shots[0].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_report_dup_merges(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    r1 = c.post("/api/assistant/report", json={"desc": "收件箱一直转圈加载失败"})
    r2 = c.post("/api/assistant/report", json={"desc": "收件箱一直转圈加载失败"})
    assert r1.json()["dup"] is False
    assert r2.json()["dup"] is True
    assert r2.json()["ticket_id"] == r1.json()["ticket_id"]


def test_report_rejects_bad_attachment_and_short_desc(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    assert c.post("/api/assistant/report", json={"desc": "短"}).status_code == 400
    # 非图片字节（JSON 信封当截图交上来 = 媒体验证纪律要拦的经典形态）
    bad = base64.b64encode(b'{"ok":true,"audio_base64":"xx"}').decode()
    r = c.post("/api/assistant/report",
               json={"desc": "有问题的截图上传", "shot_b64": bad})
    assert r.status_code == 400
    # 超限
    huge = base64.b64encode(_PNG + b"\x00" * (70 * 1024)).decode()
    r2 = c.post("/api/assistant/report",
                json={"desc": "超大截图上传测试", "shot_b64": huge})
    assert r2.status_code == 400


def test_report_jpg_accepted(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    shot = base64.b64encode(_JPG).decode()
    r = c.post("/api/assistant/report",
               json={"desc": "贴一张 jpg 截图的报障", "shot_b64": shot})
    assert r.status_code == 200


# ────────────────────────────────────────────────────────── tickets
def test_tickets_scoped_to_self_and_panel_notify_stamp(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    tid = c.post("/api/assistant/report",
                 json={"desc": "语音功能坏了要修一下"}).json()["ticket_id"]
    # 别人的单（直接落库）不可见
    from src.ops.bug_intake import record_bug_ticket, set_ticket_status

    other = record_bug_ticket(chat_id="webuser:someone-else", account_id="x",
                              reporter_id="z", reporter_name="z",
                              text="别人的问题")
    rows = c.get("/api/assistant/tickets").json()["tickets"]
    assert [r["id"] for r in rows] == [tid]
    # fixed 后拉取 = 面板即回访，盖 notify_ts
    assert set_ticket_status(tid, "fixed")
    rows2 = c.get("/api/assistant/tickets").json()["tickets"]
    assert rows2[0]["status"] == "fixed" and float(rows2[0]["notify_ts"]) > 0
    from src.ops.bug_intake import get_ticket

    assert float(get_ticket(tid)["notify_ts"]) > 0
    assert get_ticket(other["ticket_id"])["notify_ts"] == 0


def test_pending_notify_excludes_webuser(monkeypatch, tmp_path):
    """TG 回访冲刷队列必须排除 web 工单（防串味守卫的行为钉）。"""
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "d2"))
    from src.ops import bug_intake

    bug_intake.reset_state_for_tests()
    tg = bug_intake.record_bug_ticket(chat_id="-100123", account_id="a",
                                      reporter_id="1", reporter_name="张三",
                                      text="tg 工单")
    web = bug_intake.record_bug_ticket(chat_id="webuser:u9", account_id="assistant",
                                       reporter_id="u9", reporter_name="op",
                                       text="web 工单")
    bug_intake.set_ticket_status(tg["ticket_id"], "fixed")
    bug_intake.set_ticket_status(web["ticket_id"], "fixed")
    pend = bug_intake.list_pending_notify()
    ids = [int(r["id"]) for r in pend]
    assert tg["ticket_id"] in ids and web["ticket_id"] not in ids
    stats = bug_intake.dump_stats()
    assert stats["pending_notify"] == 1


# ────────────────────────────────────────────────────────── feedback / health
def test_feedback_writes_verdict(monkeypatch, tmp_path):
    app, _ai, log, _kb, stats = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    r = c.post("/api/assistant/query", json={"q": "怎么发语音"})
    qa_id = _stream_events(r)[-1]["qa_id"]
    assert c.post("/api/assistant/feedback",
                  json={"qa_id": qa_id, "verdict": "down"}).json()["ok"]
    assert c.post("/api/assistant/feedback",
                  json={"qa_id": qa_id, "verdict": "meh"}).status_code == 400
    assert log.stats(days=1)["down"] == 1
    assert stats.dump()["feedback_down"] == 1


def test_health_shape(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    j = c.get("/api/assistant/health").json()
    assert j["ok"] and j["enabled"] and j["kb_entries"] == 1
    assert j["llm_wired"] is True
    assert "qa_7d" in j and "miss_top" in j and "process" in j


# ────────────────────────────────────────────────────────── 静态接线钉
def test_agent_whitelist_includes_assistant_and_telemetry():
    """admin.py agent API 白名单必须含 /api/assistant 与 /api/telemetry
    （后者顺修坐席 beacon 403 静默丢失的存量缺陷）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "web"
           / "admin.py").read_text(encoding="utf-8")
    assert 'path.startswith("/api/assistant")' in src
    assert 'path.startswith("/api/telemetry")' in src


def test_admin_registers_assistant_routes():
    src = (Path(__file__).resolve().parents[1] / "src" / "web"
           / "admin.py").read_text(encoding="utf-8")
    assert "register_assistant_routes" in src
    assert "/assistant-shared" in src


def test_i18n_pack_bilingual():
    from src.web.i18n_packs.assistant_ball import EN, ZH

    assert set(ZH.keys()) == set(EN.keys())
    # asb.* = 路由文案；ov2_* = 本功能的 ops-overview 卡（同域词条同 pack 管理）
    assert all(k.startswith(("asb.", "ov2_")) for k in ZH)


# ────────────────────────────────────────────────────────── transcribe (P1)
def _fake_transcribe(monkeypatch, text="怎么发语音"):
    import src.ai.voice_translate as vt

    async def _fn(path):
        return text

    monkeypatch.setattr(vt, "build_audio_transcribe_fn",
                        lambda cfg, **kw: _fn)


def _webm_b64() -> str:
    return ("data:audio/webm;base64,"
            + base64.b64encode(b"\x1aE\xdf\xa3" + b"\x00" * 4096).decode())


def test_transcribe_disabled_by_default(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path)  # voice 缺省 False
    c = _client(app)
    r = c.post("/api/assistant/transcribe", json={"audio_b64": _webm_b64()})
    assert r.status_code == 403
    j = c.get("/api/assistant/bootstrap").json()
    assert j["voice"] is False


def test_transcribe_roundtrip(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path, voice=True)
    _fake_transcribe(monkeypatch, "知识库怎么加条目")
    c = _client(app)
    assert c.get("/api/assistant/bootstrap").json()["voice"] is True
    r = c.post("/api/assistant/transcribe", json={"audio_b64": _webm_b64()})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "text": "知识库怎么加条目"}


def test_transcribe_bad_audio_400(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path, voice=True)
    c = _client(app)
    assert c.post("/api/assistant/transcribe",
                  json={"audio_b64": ""}).status_code == 400
    assert c.post("/api/assistant/transcribe",
                  json={"audio_b64": "data:video/mp4;base64,QUFB"}
                  ).status_code == 400


def test_transcribe_asr_unconfigured_503(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path, voice=True, audio_pipeline=False)
    c = _client(app)
    r = c.post("/api/assistant/transcribe", json={"audio_b64": _webm_b64()})
    assert r.status_code == 503


# ────────────────────────────────────────────────────────── vision 摘要 (P1)
def _png_b64() -> str:
    return "data:image/png;base64," + base64.b64encode(_PNG).decode()


def test_report_vision_summary_appended(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path, vision=True)
    import src.vision_client as vc

    async def _fake(*a, **k):
        return "画面里有一条红色错误横幅", "stub"

    monkeypatch.setattr(vc.VisionClient,
                        "describe_image_with_ollama_zhipu_fallback", _fake)
    c = _client(app)
    r = c.post("/api/assistant/report",
               json={"desc": "语音按钮点了没反应", "page": "/workspace",
                     "shot_b64": _png_b64()})
    assert r.status_code == 200
    tid = r.json()["ticket_id"]
    from src.ops.bug_intake import get_ticket

    body = str((get_ticket(tid) or {}).get("body") or "")
    assert "AI 视觉摘要" in body and "红色错误横幅" in body


def test_report_vision_failure_never_blocks(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path, vision=True)
    import src.vision_client as vc

    async def _boom(*a, **k):
        raise RuntimeError("vlm down")

    monkeypatch.setattr(vc.VisionClient,
                        "describe_image_with_ollama_zhipu_fallback", _boom)
    c = _client(app)
    r = c.post("/api/assistant/report",
               json={"desc": "语音按钮点了没反应", "page": "/workspace",
                     "shot_b64": _png_b64()})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_report_vision_off_no_vlm_call(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path)  # vision 缺省 False
    import src.vision_client as vc

    calls = {"n": 0}

    async def _spy(*a, **k):
        calls["n"] += 1
        return "x", "stub"

    monkeypatch.setattr(vc.VisionClient,
                        "describe_image_with_ollama_zhipu_fallback", _spy)
    c = _client(app)
    r = c.post("/api/assistant/report",
               json={"desc": "语音按钮点了没反应", "page": "/workspace",
                     "shot_b64": _png_b64()})
    assert r.status_code == 200 and calls["n"] == 0


def test_transcribe_empty_text_422(monkeypatch, tmp_path):
    app, *_ = _mk_app(monkeypatch, tmp_path, voice=True)
    _fake_transcribe(monkeypatch, "")
    c = _client(app)
    r = c.post("/api/assistant/transcribe", json={"audio_b64": _webm_b64()})
    assert r.status_code == 422


# ────────────────────────────────────────────────────── anchor 透传 (P3)
def test_query_sources_carry_anchor(monkeypatch, tmp_path):
    """「带我去」聚光灯契约：help_kb.anchor 经 meta.sources 透传到前端；
    无锚点条目回空串（前端只跳页不聚光）。"""
    app, *_ = _mk_app(monkeypatch, tmp_path)
    c = _client(app)
    r = c.post("/api/assistant/query",
               json={"q": "怎么发语音", "page": "/workspace"})
    evs = _stream_events(r)
    src = evs[0]["sources"][0]
    assert src["id"] == "t1"
    assert src["anchor"] == "#t1-anchor"


def test_help_kb_anchor_migration_idempotent(monkeypatch, tmp_path):
    """旧库（无 anchor 列）→ HelpKB 打开即幂等补列；重开不重复不报错。"""
    import sqlite3

    from src.assistant.help_kb import HelpKB

    db = tmp_path / "old_help.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE help_entries ("
        "id TEXT PRIMARY KEY, title TEXT NOT NULL,"
        "title_en TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '',"
        "content_en TEXT NOT NULL DEFAULT '', keywords TEXT NOT NULL DEFAULT '',"
        "source TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',"
        "enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT '')")
    con.execute(
        "INSERT INTO help_entries(id,title,keywords) VALUES('old1','旧条目','旧')")
    con.commit()
    con.close()

    kb = HelpKB(db)          # 打开即迁移
    kb2 = HelpKB(db)         # 二次打开=幂等
    assert kb2.count() == 1
    hits = kb.search("旧条目", top_k=1)
    assert hits and hits[0]["anchor"] == ""  # 旧行回空串不炸
    kb.upsert_entries([{"id": "old1", "title": "旧条目",
                        "keywords": "旧", "anchor": "#x"}])
    assert kb.search("旧条目", top_k=1)[0]["anchor"] == "#x"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
