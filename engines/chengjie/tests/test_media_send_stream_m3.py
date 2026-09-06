# -*- coding: utf-8 -*-
"""M-3 A/B（#227 #229 #231，2026-09-06）：send-media 上传链——2MB 闸豁免 / 裸流落盘 /
上限同源（视频压 50MB）/ .MOV 换封装或 415 / 访问日志 / send-caps 新键。

根因（本机 uvicorn 复现 ConnectionAbortedError 10053）：admin.py 的全局 body 闸默认 2MB，
send-media 从未在豁免/覆写表里 → 任何 >2MB 的图/视频在路由跑起来之前就被 413 +
Connection:close，浏览器还在推 body 时连接被掐，XHR 只见 onerror、后端零 [send-media]
记录 → 坐席看到「结果未知」。98MB 视频、5MB .MOV 三平台全灭、今日成功视频全是 <2MB
的 .MP4，都是它。
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from src.inbox import media_limits as ML
from src.integrations import protocol_bridge as PB

_ROOT = Path(__file__).resolve().parents[1]
_ADMIN = _ROOT / "src" / "web" / "admin.py"
TOKEN = "m3-test-token"

#: 最小合法 MP4 头（ftyp box，magic 守卫认 mp4 家族）+ 填充
_MP4_HEAD = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


def _fake_video_bytes(size: int) -> bytes:
    return _MP4_HEAD + b"\x00" * max(0, size - len(_MP4_HEAD))


# ─────────────────── 1. 上限同源：视频类临时压顶 ───────────────────

def test_video_cap_pressed_to_50_by_default_only_for_video():
    assert ML.DEFAULT_VIDEO_CAP_MB == 50
    assert ML.media_cap_mb_for(None, "telegram", "video") == 50
    assert ML.media_cap_mb_for(None, "line", "video") == 50
    # 平台表本身低于 50 的不受影响（取 min）
    assert ML.media_cap_mb_for(None, "messenger", "video") == 25
    # 非视频类＝平台上限原值
    assert ML.media_cap_mb_for(None, "telegram", "image") == 200
    assert ML.media_cap_mb_for(None, "line", "document") == 100


def test_video_cap_config_override_and_disable():
    cfg = {"inbox": {"media": {"video_limit_mb": 80}}}
    assert ML.media_cap_mb_for(cfg, "telegram", "video") == 80
    assert ML.media_cap_mb_for(cfg, "whatsapp", "video") == 64   # 平台 64 < 80
    off = {"inbox": {"media": {"video_limit_mb": 0}}}
    assert ML.media_cap_mb_for(off, "telegram", "video") == 200  # 0＝不压，回平台表
    assert ML.media_cap_mb_for({"inbox": {"media": {"video_limit_mb": "x"}}}, "telegram", "video") == 50


# ─────────────────── 2. 2MB 全局闸：send-media 代码级豁免 ───────────────────

def test_body_limit_exempts_send_media_in_code():
    src = _ADMIN.read_text(encoding="utf-8")
    i = src.find("_BODY_LIMIT_CODE_EXEMPT_PREFIXES")
    assert i > 0, "豁免表缺失"
    assert '"/api/unified-inbox/send-media"' in src[i:i + 400]
    # 访问日志中间件在 body 闸之后注册（＝外层），被闸拒的请求也落痕
    assert src.find("upload_access_log_middleware") > src.find("async def body_size_limit_middleware")


# ─────────────────── 3. 路由：裸流 / multipart / 413 / 415 / send-caps ───────────────────

class _FakeOrch:
    def __init__(self):
        self.sent = []

    def media_capability(self, platform, account_id):
        return {"owns": True, "reason": ""}

    def owns(self, platform, account_id):
        return True

    async def send_media(self, platform, account_id, chat_key, *, media_path, media_url,
                         media_type, caption="", **kw):
        import os
        self.sent.append({"platform": platform, "chat_key": chat_key, "media_type": media_type,
                          "path": media_path, "size": os.path.getsize(media_path),
                          "url": media_url})
        return {"ok": True, "delivered": True, "message_id": "fake"}


@pytest.fixture()
def app_and_orch(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "data"))
    from src.utils.audit_store import AuditStore
    from src.utils.config_manager import ConfigManager
    from src.web.admin import create_app
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "payment",
        "domain_plugins": {"payment": {"enabled": True}},
        "web_admin": {"secret_key": "test-secret-very-long-key-for-testing",
                      "auth_token": TOKEN, "session_max_age": 3600},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    asyncio.run(cm.load())
    app = create_app(cm, audit_store=AuditStore(db_path=tmp_path / "audit.db"), boot_ts=0,
                     telegram_client=None, event_tracker=None, log_buffer=None)
    fake = _FakeOrch()
    import src.integrations.account_orchestrator as AO
    monkeypatch.setattr(AO, "get_orchestrator", lambda *a, **k: fake)
    monkeypatch.setattr(AO, "get_orchestrator_if_running", lambda *a, **k: fake)
    return app, fake


def _q(**kw):
    import uuid
    from urllib.parse import urlencode
    # 幂等键进程内单例：同 (scope, id) 第二次就是 duplicate → 每次现造
    base = {"platform": "telegram", "account_id": "acc1", "chat_key": "peer1", "caption": "",
            "name": "clip.mp4", "client_msg_id": "cm-" + uuid.uuid4().hex}
    base.update(kw)
    return "/api/unified-inbox/send-media?" + urlencode(base)


def _hdr(mime="video/mp4"):
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": mime}


def test_stream_upload_over_2mb_reaches_route_and_dispatches(app_and_orch):
    """5MB 裸流：过 2MB 闸、流式落盘、投递——修前这里是 413 + 连接被掐。"""
    app, fake = app_and_orch
    data = _fake_video_bytes(5 * 1024 * 1024)
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post(_q(), content=data, headers=_hdr())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["media_type"] == "video"
    assert len(fake.sent) == 1 and fake.sent[0]["size"] == len(data)
    assert fake.sent[0]["path"].endswith(".mp4")


def test_multipart_upload_over_2mb_still_works(app_and_orch):
    """旧前端 multipart 路径保留：同样过闸、同样投递。"""
    app, fake = app_and_orch
    data = _fake_video_bytes(3 * 1024 * 1024)
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post("/api/unified-inbox/send-media",
                   data={"platform": "telegram", "account_id": "acc1", "chat_key": "peer1",
                         "caption": "", "client_msg_id": "cm-mp"},
                   files={"file": ("clip.mp4", data, "video/mp4")},
                   headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    assert len(fake.sent) == 1 and fake.sent[0]["size"] == len(data)


def test_stream_upload_video_over_pressed_cap_is_413_with_real_size(app_and_orch):
    """98MB 视频：Content-Length 一到手就 413，文案带实际大小与 50MB 上限；不落盘不投递。"""
    app, fake = app_and_orch
    size = 98 * 1024 * 1024
    data = _fake_video_bytes(size)
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post(_q(name="product_mv_16x9.mp4"), content=data, headers=_hdr())
    assert r.status_code == 413, r.text
    body = r.text
    assert "98.0" in body and "50" in body
    assert fake.sent == []


def test_stream_upload_image_not_pressed_by_video_cap(app_and_orch):
    """图片不吃视频压顶：60MB 的 png 走 telegram 200MB 平台上限（这里只验不被 50 拦）。"""
    app, fake = app_and_orch
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * (60 * 1024 * 1024)
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post(_q(name="big.png"), content=png, headers=_hdr("image/png"))
    assert r.status_code == 200, r.text
    assert fake.sent[0]["media_type"] == "image"


def test_stream_upload_mov_without_ffmpeg_is_415(app_and_orch, monkeypatch):
    app, fake = app_and_orch
    monkeypatch.setattr(PB, "video_transcode_available", lambda: False)
    data = _fake_video_bytes(1024 * 1024)
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post(_q(name="IMG_7312.MOV"), content=data, headers=_hdr("video/quicktime"))
    assert r.status_code == 415, r.text
    assert ".mov" in r.text.lower()
    assert fake.sent == []


def test_stream_upload_mov_remuxed_to_mp4_when_ffmpeg_available(app_and_orch, monkeypatch):
    """有 ffmpeg：.MOV 落盘后换封装成 .mp4 再投递（这里用假 remux 只验接线与产物名）。"""
    app, fake = app_and_orch
    monkeypatch.setattr(PB, "video_transcode_available", lambda: True)

    def _fake_remux(src, dst, **kw):
        Path(dst).write_bytes(Path(src).read_bytes())
        return True, ""

    monkeypatch.setattr(PB, "remux_video_to_mp4", _fake_remux)
    data = _fake_video_bytes(1024 * 1024)
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post(_q(name="IMG_7312.MOV"), content=data, headers=_hdr("video/quicktime"))
    assert r.status_code == 200, r.text
    assert fake.sent[0]["path"].endswith(".mp4") and fake.sent[0]["url"].endswith(".mp4")
    assert not Path(fake.sent[0]["path"]).with_suffix(".mov").exists()


def test_stream_upload_magic_mismatch_is_415(app_and_orch):
    app, fake = app_and_orch
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post(_q(name="x.mp4"), content=b"\x00" * 4096, headers=_hdr())
    assert r.status_code == 415
    assert fake.sent == []


def test_send_caps_exposes_same_source_keys(app_and_orch):
    app, _ = app_and_orch
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.get("/api/unified-inbox/send-caps?platform=telegram&account_id=acc1",
                  headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["media_max_mb"] == 200
    assert d["video_max_mb"] == 50
    assert d["video_exts"] == [".mp4", ".webm"]
    assert d["media_stream_upload"] is True
    # ffmpeg 有无决定 transcode 表是否为空，但键必在
    assert isinstance(d["video_transcode_exts"], list)


# ─────────────────── 4. protocol_bridge：容器口径 + 换封装钩子 ───────────────────

def test_video_ext_tables_consistent():
    assert PB.OUT_VIDEO_NATIVE_EXT <= frozenset(PB._OUT_VIDEO_EXT)
    assert PB.OUT_VIDEO_TRANSCODE_EXT <= frozenset(PB._OUT_VIDEO_EXT)
    assert not (PB.OUT_VIDEO_NATIVE_EXT & PB.OUT_VIDEO_TRANSCODE_EXT)
    assert PB.media_type_from_ext(".mov") == "video" and PB.media_type_from_ext(".m4v") == "video"


def test_remux_without_ffmpeg_degrades_cleanly(monkeypatch, tmp_path):
    import src.utils.ffmpeg_resolver as FR
    monkeypatch.setattr(FR, "ffmpeg_path", lambda: None)
    ok, why = PB.remux_video_to_mp4(str(tmp_path / "a.mov"), str(tmp_path / "a.mp4"))
    assert ok is False and why == "ffmpeg_missing"
    assert PB.video_transcode_available() is False


def test_route_logs_every_exit_path():
    """每条退出路径都有 [send-media] 记录（8YNDKE「后端零记录」不再发生在路由层）。"""
    from src.web.routes import unified_inbox_send_routes as R
    src = inspect.getsource(R._send_media_streamed)
    for reason in ("too_large", "content_guard", "video_format_unsupported",
                   "video_transcode_failed", "recv_error", "dispatch_error", "undelivered"):
        assert f'"{reason}"' in src, reason
    assert "[send-media] ok " in src and "declared=" in src
