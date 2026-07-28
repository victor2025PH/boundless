"""tts-test「后台 job 模式」契约门禁（channel-center 后台试听）。

Hermetic 离线（fixture 模式复用 tests/test_voice_tts_fast_preview.py）：假 TTSPipeline
（asyncio.sleep 模拟合成耗时，不碰真 TTS/GPU/网络）、PersonaManager 打断（不依赖真人设
文件）、licensing 配额放行、预览目录指向 tmp_path、模块级 job 表逐测清零。

与前端约定的契约（键名一字不差）：
- POST /api/voice/tts-test body.background=true → 入参校验/配额检查同步完成后立即
  {ok:true, job_id:"<uuid hex>"}，「resolve voice_cfg → override → fast → 合成 →
  整形响应」整段进后台任务；running 状态 job ≥3 → {ok:false,error:"busy"}。
  不带 background / background=false → 同步等待，行为与既有 7 例逐字节一致（回归护栏
  = tests/test_voice_tts_fast_preview.py 与本文件一起跑）。
- GET /api/voice/tts-test-jobs/{job_id} →
  running: {ok:true,status:"running",age_sec:n}
  done:    {ok:true,status:"done",result:{…与同步成功响应完全同构…}}
  失败:    {ok:true,status:"error",error:"…同步路径会返回的 error 文案…"}
  不存在/超 900s 过期: {ok:false,error:"not_found"}（GET/POST 路径顺手清理过期条目）。

注意：TestClient 必须以 with 上下文使用（持久 portal = 跨请求同一事件循环），
后台 asyncio.create_task 的任务才能在轮询间隙真正跑完；裸 TestClient 每请求
新开事件循环，请求结束任务即被取消，job 会永远停在 running。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.web.routes.voice_routes as voice_routes_mod
from src.web.routes.voice_routes import register_voice_routes


def _noop_auth(request: Request):
    return None


class _CfgMgr:
    """最小 config_manager 替身：voice_routes 只读 .config。"""

    def __init__(self, config):
        self.config = config


def _base_config():
    return {
        "telegram": {"voice_reply": {
            "backend": "avatar_clone",
            "voice": "lin_xiaoyu",
            "voice_profile": {
                "enabled": True,
                "speaker_id": "lin_xiaoyu",
                "reference_audio_path": "assets/voices/lin/ref.wav",
            },
        }},
        "whatsapp_rpa": {"voice_output": {"backend": "edge_tts"}},
    }


class _FakeTTSResult:
    """形状对齐 voice_routes 消费的 TTSResult 字段。"""

    def __init__(self, audio_path: str):
        self.ok = True
        self.audio_path = audio_path
        self.error = ""
        self.provider = "fake_edge"
        self.voice = "fake-voice"
        self.format = "mp3"
        self.duration_sec = 1.5
        self.extra = {}


class _FakeTTSPipeline:
    """asyncio.sleep(delay) 模拟合成耗时；真写一个小音频文件供 rename/stat。"""

    calls: list = []
    delay: float = 0.05

    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})

    async def synthesize(self, text, **kwargs):
        type(self).calls.append({"cfg": dict(self.cfg), "kwargs": dict(kwargs)})
        await asyncio.sleep(type(self).delay)
        out_dir = Path(self.cfg.get("out_dir") or ".")
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"fake-{len(type(self).calls)}.mp3"
        p.write_bytes(b"0" * 1024)
        return _FakeTTSResult(str(p))


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    _FakeTTSPipeline.calls = []
    _FakeTTSPipeline.delay = 0.05
    # voice_routes 在函数体内延迟 `from src.ai.tts_pipeline import TTSPipeline`
    # → monkeypatch 打在源模块属性上即可生效。
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _FakeTTSPipeline)
    # licensing 配额放行（提交时同步检查的那一次）
    monkeypatch.setattr("src.licensing.quota_store.check_license_quota",
                        lambda *a, **k: {"allowed": True})

    # 不依赖真人设文件：PersonaManager 一律抛错 → persona_voice 各层走文档化兜底
    from src.utils.persona_manager import PersonaManager

    def _boom(*a, **k):
        raise RuntimeError("no persona files in hermetic test")

    monkeypatch.setattr(PersonaManager, "get_instance", classmethod(_boom))
    # 预览文件写进 tmp_path，不污染生产 tmp_tts_preview/
    monkeypatch.setattr(voice_routes_mod, "_TTS_PREVIEW_DIR", tmp_path / "previews")
    # 模块级 job 表逐测清零（防跨测串味）
    voice_routes_mod._TTS_JOBS.clear()
    yield
    voice_routes_mod._TTS_JOBS.clear()


@pytest.fixture
def client():
    app = FastAPI()
    register_voice_routes(app, api_auth=_noop_auth,
                          config_manager=_CfgMgr(_base_config()))
    # with 上下文 = 持久 portal：跨请求共用同一事件循环，后台任务在
    # 轮询间隙持续推进；上下文退出时取消未完任务（不泄漏到下个测试）。
    with TestClient(app) as c:
        yield c


def _poll_until_settled(client, job_id, deadline_sec=5.0):
    """轮询 GET 直到 job 离开 running（TestClient 同步 → sleep+GET 循环）。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_sec:
        body = client.get(f"/api/voice/tts-test-jobs/{job_id}").json()
        if not body.get("ok") or body.get("status") != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} still running after {deadline_sec}s")


# ── ① 提交秒回 job_id → 轮询到 done，result 与同步成功响应同构 ─────────────


def test_background_submit_then_done_result_isomorphic_to_sync(client):
    # 同步基准（同 fixtures 同 body，不带 background）
    sync_body = client.post("/api/voice/tts-test",
                            json={"text": "你好呀", "fast": True}).json()
    assert sync_body["ok"] is True

    r = client.post("/api/voice/tts-test",
                    json={"text": "你好呀", "fast": True, "background": True})
    assert r.status_code == 200
    sub = r.json()
    assert sub["ok"] is True
    assert set(sub) == {"ok", "job_id"}      # 秒回，不带合成产物
    job_id = sub["job_id"]
    assert isinstance(job_id, str) and len(job_id) == 32
    int(job_id, 16)                          # uuid hex

    final = _poll_until_settled(client, job_id)
    assert set(final) == {"ok", "status", "result"}
    assert final["ok"] is True
    assert final["status"] == "done"
    result = final["result"]

    # 与同步成功响应完全同构：键集一致 + 关键键逐一断言
    assert set(result) == set(sync_body)
    for key in ("url", "audio_url", "filename", "duration_sec", "provider",
                "voice", "format", "bytes", "voice_meta", "fast",
                "requested_backend"):
        assert key in result
    assert result["ok"] is True
    assert result["fast"] is True
    assert result["requested_backend"] == "avatar_clone"
    assert result["provider"] == "fake_edge"
    assert result["voice"] == "fake-voice"
    assert result["format"] == "mp3"
    assert result["duration_sec"] == 1.5
    assert result["bytes"] == 1024
    assert result["filename"].startswith("ttspreview-")
    assert result["url"] == f"/api/voice/tts-test/{result['filename']}"
    assert result["audio_url"] == result["url"]
    assert set(result["voice_meta"]) == set(sync_body["voice_meta"])
    # 稳定字段与同步响应逐一相等（filename/url 系随机 uuid，不比）
    for key in ("provider", "voice", "format", "fast", "requested_backend",
                "duration_sec", "bytes"):
        assert result[key] == sync_body[key]

    # 后台产物真实落盘且可被文件端点服务
    served = client.get(result["url"])
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("audio/")
    assert len(served.content) == 1024

    # fast 档语义在后台任务里同样生效（假管线第 2 次调用 = 后台那次）
    bg_call = _FakeTTSPipeline.calls[-1]
    assert bg_call["cfg"]["backend"] == "edge_tts"
    assert "voice_profile" not in bg_call["cfg"]
    assert bg_call["kwargs"]["timeout_sec"] == 15.0


# ── ② 假管线抛异常 → status=error 且 error 非空（同步路径同文案）────────────


def test_background_error_paths_carry_sync_error_text(client, monkeypatch):
    async def _raise(self, text, **kwargs):
        raise RuntimeError("synth down")

    monkeypatch.setattr(_FakeTTSPipeline, "synthesize", _raise)
    sub = client.post("/api/voice/tts-test",
                      json={"text": "hi", "background": True}).json()
    assert sub["ok"] is True
    final = _poll_until_settled(client, sub["job_id"])
    assert final["ok"] is True
    assert final["status"] == "error"
    assert final["error"]                       # 非空
    assert final["error"] == "RuntimeError: synth down"  # 与同步 _exs 文案一致

    # result.ok=False（合成器如实报错不抛异常）→ 同样归 error，文案=result.error
    async def _not_ok(self, text, **kwargs):
        res = _FakeTTSResult("")
        res.ok = False
        res.error = "backend unavailable"
        return res

    monkeypatch.setattr(_FakeTTSPipeline, "synthesize", _not_ok)
    sub2 = client.post("/api/voice/tts-test",
                       json={"text": "hi", "background": True}).json()
    final2 = _poll_until_settled(client, sub2["job_id"])
    assert final2["status"] == "error"
    assert final2["error"] == "backend unavailable"


# ── ③ 不存在的 job_id → {ok:false,error:"not_found"} ───────────────────────


def test_unknown_job_id_not_found(client):
    body = client.get("/api/voice/tts-test-jobs/" + "deadbeef" * 4).json()
    assert body == {"ok": False, "error": "not_found"}


# ── ④ 3 个 running 时第 4 个提交 → busy ────────────────────────────────────


def test_fourth_concurrent_submit_rejected_busy(client):
    _FakeTTSPipeline.delay = 3.0   # 足够久：4 次提交 + 轮询远快于 3s
    ids = []
    for i in range(3):
        sub = client.post(
            "/api/voice/tts-test",
            json={"text": f"slow {i}", "background": True}).json()
        assert sub["ok"] is True
        ids.append(sub["job_id"])

    # 在跑期间轮询口如实报 running + age_sec（数值）
    st = client.get(f"/api/voice/tts-test-jobs/{ids[0]}").json()
    assert st["ok"] is True
    assert st["status"] == "running"
    assert isinstance(st["age_sec"], (int, float)) and st["age_sec"] >= 0
    assert set(st) == {"ok", "status", "age_sec"}

    sub4 = client.post("/api/voice/tts-test",
                       json={"text": "one too many", "background": True}).json()
    assert sub4 == {"ok": False, "error": "busy"}
    # 拒绝的提交不产生 job 条目
    assert set(voice_routes_mod._TTS_JOBS) == set(ids)


# ── ⑤ 不带 background 的同步请求行为不变（既有 7 例另行整跑做回归）──────────


def test_sync_request_without_background_unchanged(client):
    r = client.post("/api/voice/tts-test", json={"text": "你好呀"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["fast"] is False
    assert body["requested_backend"] == "avatar_clone"
    assert "job_id" not in body
    # 同步路径不进 job 表；假管线原样收到克隆后端（非 fast 不改写）
    assert voice_routes_mod._TTS_JOBS == {}
    call = _FakeTTSPipeline.calls[0]
    assert call["cfg"]["backend"] == "avatar_clone"
    assert isinstance(call["cfg"].get("voice_profile"), dict)
    assert call["kwargs"]["timeout_sec"] == 45.0

    # background=false 显式传入 → 同样走同步等待
    b2 = client.post("/api/voice/tts-test",
                     json={"text": "hi", "background": False}).json()
    assert b2["ok"] is True
    assert "job_id" not in b2
    assert voice_routes_mod._TTS_JOBS == {}


# ── ⑥ 过期清理：ts 篡改为 20 分钟前 → 任意一次 GET/POST 后条目消失 ──────────


def test_expired_job_swept_on_get_and_post(client):
    sub = client.post("/api/voice/tts-test",
                      json={"text": "你好", "background": True}).json()
    job_id = sub["job_id"]
    final = _poll_until_settled(client, job_id)
    assert final["status"] == "done"

    # GET 路径：篡改 ts 为 20 分钟前 → 顺手清理 → not_found 且条目消失
    voice_routes_mod._TTS_JOBS[job_id]["ts"] = time.time() - 1200
    body = client.get(f"/api/voice/tts-test-jobs/{job_id}").json()
    assert body == {"ok": False, "error": "not_found"}
    assert job_id not in voice_routes_mod._TTS_JOBS

    # POST 路径同样顺手清理（哪怕是不带 background 的同步提交）
    sub2 = client.post("/api/voice/tts-test",
                       json={"text": "再来", "background": True}).json()
    job2 = sub2["job_id"]
    assert _poll_until_settled(client, job2)["status"] == "done"
    voice_routes_mod._TTS_JOBS[job2]["ts"] = time.time() - 1200
    client.post("/api/voice/tts-test", json={"text": "同步一发"})
    assert job2 not in voice_routes_mod._TTS_JOBS
