# -*- coding: utf-8 -*-
"""#316 追加（09-13 钧 明日香语）：相册上传口豁免 2MB 全局 body 闸。

事故：人设工作室批量传 2.1–2.7MB PNG，Q-35 结果条「成功 0 / 失败 25 · 网络中断
Failed to fetch」。路由级上限是图 10MB / 视频 50MB，请求到不了
``persona_media_routes``——与 M-3 send-media 同一把闸
（413 + Connection:close，浏览器还在推 body）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from src.web.admin import is_persona_album_upload_path

_ROOT = Path(__file__).resolve().parents[1]
_ADMIN = _ROOT / "src" / "web" / "admin.py"
TOKEN = "q35-album-body-token"
_PNG = b"\x89PNG\r\n\x1a\n"


def test_persona_album_upload_path_matcher():
    assert is_persona_album_upload_path("/api/personas/lin/media")
    assert is_persona_album_upload_path("/api/personas/mingri_xiangyu/media")
    assert is_persona_album_upload_path("/api/personas/lin/face-ref")
    # JSON 口仍吃 2MB 闸
    assert not is_persona_album_upload_path("/api/personas/lin/media/test")
    assert not is_persona_album_upload_path("/api/personas/lin/media/retag-all")
    assert not is_persona_album_upload_path("/api/personas/lin/media/abc/retag")
    assert not is_persona_album_upload_path("/api/personas/lin/speech-print")
    assert not is_persona_album_upload_path("/api/unified-inbox/send-media")
    assert not is_persona_album_upload_path("")


def test_body_limit_middleware_calls_album_matcher():
    src = _ADMIN.read_text(encoding="utf-8")
    i = src.find("async def body_size_limit_middleware")
    assert i > 0
    chunk = src[i:i + 800]
    assert "is_persona_album_upload_path(path)" in chunk
    assert '"/api/unified-inbox/send-media"' in src


@pytest.fixture()
def app(tmp_path, monkeypatch):
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
    return create_app(cm, audit_store=AuditStore(db_path=tmp_path / "audit.db"), boot_ts=0,
                      telegram_client=None, event_tracker=None, log_buffer=None)


def test_album_upload_over_2mb_is_not_middleware_413(app):
    """2.5MB PNG：修前是全局闸 413 max_body_bytes=2MB；修后应进路由（人设不存在=404）。"""
    data = _PNG + b"\x00" * (int(2.5 * 1024 * 1024) - len(_PNG))
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post(
            "/api/personas/lin/media",
            files={"file": ("lin-yourou-selfie-01.png", data, "image/png")},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert r.status_code != 413 or "max_body_bytes" not in r.text, r.text
    assert r.status_code in (200, 400, 403, 404), r.text


def test_album_json_side_routes_still_eat_2mb_gate(app):
    """/media/test 不是上传口，2.5MB 仍应被全局闸拦住。"""
    blob = b"x" * (int(2.5 * 1024 * 1024))
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.post(
            "/api/personas/lin/media/test",
            content=blob,
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
    assert r.status_code == 413, r.text
    assert r.json().get("max_body_bytes") == 2 * 1024 * 1024
