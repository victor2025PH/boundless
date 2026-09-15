"""每人设相册后台 API 契约门禁：上传/列表/改/删/试触发 + 护栏（扩展名/体积/去重/404）。"""
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.web.routes.persona_media_routes as pmr
from src.companion.persona_media_store import (
    configure_persona_media_store, get_persona_media_store,
    reset_persona_media_store)
from src.utils.persona_manager import PersonaManager


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")  # 不写 repo static
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("lin", {"name": "Lin"})
    app = FastAPI()
    pmr.register_persona_media_routes(app, auth_dep=lambda: True, config_manager=None)
    yield TestClient(app)
    reset_persona_media_store()
    pm.delete_profile("lin")


class _FakeAudit:
    def __init__(self):
        self.entries = []

    def log(self, user_id, action, target="", old_val="", new_val="", snapshot_id=""):
        self.entries.append((user_id, action, target, new_val))


@pytest.fixture()
def audited_client(tmp_path, monkeypatch):
    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("lin", {"name": "Lin"})
    audit = _FakeAudit()
    app = FastAPI()
    pmr.register_persona_media_routes(
        app, auth_dep=lambda: True, audit_store=audit, config_manager=None)
    yield TestClient(app), audit
    reset_persona_media_store()
    pm.delete_profile("lin")


# #67-② 起上传带内容校验：假字节必须带真 magic（PNG 8 字节头 / mp4 ftyp box），
# 否则会被 sniff_media_bytes 如实拒收——这正是坏文件不再静默落盘的门禁本体。
_PNG = b"\x89PNG\r\n\x1a\n"
_MP4 = b"\x00\x00\x00\x18ftypmp42"


def _upload(client, *, name="a.jpg", data=_PNG + b"dummy", **fields):
    return client.post(
        "/api/personas/lin/media",
        files={"file": (name, data, "application/octet-stream")}, data=fields)


def test_list_empty(client):
    r = client.get("/api/personas/lin/media")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["stats"]["total"] == 0


# ── face_ref 锁脸基准照管理（Phase16）────────────────────────────────────

@pytest.fixture()
def face_client(tmp_path, monkeypatch):
    """带可控 album_dir 的客户端：config_manager 指向 tmp 目录（不碰 repo assets）。"""
    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("lin", {"name": "Lin"})

    class _CfgMgr:
        config = {"companion": {"selfie": {"provider": {
            "album_dir": str(tmp_path / "persona_media")}}}}
        config_path = str(tmp_path / "config" / "config.yaml")

    app = FastAPI()
    pmr.register_persona_media_routes(
        app, auth_dep=lambda: True, config_manager=_CfgMgr())
    yield TestClient(app), tmp_path / "persona_media"
    reset_persona_media_store()
    pm.delete_profile("lin")


def test_face_ref_lifecycle(face_client):
    client, album = face_client
    # 初始不存在
    r = client.get("/api/personas/lin/face-ref")
    assert r.status_code == 200 and r.json()["exists"] is False
    # 上传
    r = client.post("/api/personas/lin/face-ref",
                    files={"file": ("me.png", b"\x89PNGface", "image/png")})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (album / "lin" / "face_ref.png").is_file()
    # 状态 + 预览
    d = client.get("/api/personas/lin/face-ref").json()
    assert d["exists"] is True and d["filename"] == "face_ref.png"
    img = client.get("/api/personas/lin/face-ref/image")
    assert img.status_code == 200 and img.content == b"\x89PNGface"
    # 换扩展名替换 → 旧 face_ref.png 被清理（防 reference_image 选择歧义）
    r = client.post("/api/personas/lin/face-ref",
                    files={"file": ("new.jpg", b"\xff\xd8jpg", "image/jpeg")})
    assert r.status_code == 200
    assert not (album / "lin" / "face_ref.png").exists()
    assert (album / "lin" / "face_ref.jpg").is_file()
    # 删除
    assert client.delete("/api/personas/lin/face-ref").json()["existed"] is True
    assert client.get("/api/personas/lin/face-ref").json()["exists"] is False


def test_face_ref_guards(face_client):
    client, _ = face_client
    # 扩展名白名单（gif 不适合做锁脸参考）
    r = client.post("/api/personas/lin/face-ref",
                    files={"file": ("x.gif", b"gif", "image/gif")})
    assert r.status_code == 400
    # 未知人设 404
    r = client.post("/api/personas/nobody/face-ref",
                    files={"file": ("a.png", b"\x89PNG", "image/png")})
    assert r.status_code == 404


def test_upload_photo_with_triggers(client):
    r = _upload(client, triggers="跳舞,dance", caption="看我跳~", weight="3")
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["media_type"] == "photo"
    assert item["triggers"] == ["跳舞", "dance"] and item["caption"] == "看我跳~"
    assert item["weight"] == 3
    assert item["url"].startswith("/static/persona_albums/lin/")
    assert Path(item["file_path"]).is_file()  # 真落盘
    # 出现在列表
    assert client.get("/api/personas/lin/media").json()["stats"]["photo"] == 1


def test_upload_video_ext(client):
    r = _upload(client, name="clip.mp4", data=_MP4 + b"-dummy", triggers="跳舞")
    assert r.json()["item"]["media_type"] == "video"


def test_upload_video_probes_metadata(client, monkeypatch):
    monkeypatch.setattr(pmr, "_probe_video",
                        lambda p: {"duration_ms": 4200, "width": 720, "height": 1280})
    monkeypatch.setattr(pmr, "_make_video_thumbnail",
                        lambda src, out, **kw: (Path(out).write_bytes(b"jpg"), True)[1])
    item = _upload(client, name="clip.mp4", data=_MP4 + b"-dummy").json()["item"]
    assert item["duration_ms"] == 4200 and item["width"] == 720 and item["height"] == 1280
    assert item["thumb_url"].endswith(".thumb.jpg")
    assert Path(item["file_path"] + ".thumb.jpg").is_file()  # 封面真落盘


def test_upload_video_too_long_rejected(client, monkeypatch):
    monkeypatch.setattr(pmr, "_MAX_VIDEO_DURATION_MS", 1000)
    monkeypatch.setattr(pmr, "_probe_video",
                        lambda p: {"duration_ms": 5000, "width": 1, "height": 1})
    r = _upload(client, name="long.mp4", data=_MP4 + b"-dummy")
    assert r.status_code == 413
    assert client.get("/api/personas/lin/media").json()["stats"]["total"] == 0  # 未落库
    # 超长视频文件已回收（相册目录内无残留 .mp4）
    alb = pmr._ALBUM_ROOT / "lin"
    assert not any(p.suffix == ".mp4" for p in alb.glob("*")) if alb.is_dir() else True


def test_delete_removes_video_thumbnail(client, monkeypatch):
    monkeypatch.setattr(pmr, "_probe_video",
                        lambda p: {"duration_ms": 3000, "width": 10, "height": 10})
    monkeypatch.setattr(pmr, "_make_video_thumbnail",
                        lambda src, out, **kw: (Path(out).write_bytes(b"jpg"), True)[1])
    item = _upload(client, name="clip.mp4", data=_MP4 + b"-dummy").json()["item"]
    thumb = Path(item["file_path"] + ".thumb.jpg")
    assert thumb.is_file()
    client.delete(f"/api/personas/lin/media/{item['id']}")
    assert not thumb.exists() and not Path(item["file_path"]).exists()


def test_upload_dedup_same_bytes(client):
    r1 = _upload(client, data=_PNG + b"same-bytes")
    r2 = _upload(client, data=_PNG + b"same-bytes")
    assert r2.json().get("deduped") is True
    assert r2.json()["item"]["id"] == r1.json()["item"]["id"]
    assert client.get("/api/personas/lin/media").json()["stats"]["total"] == 1


def test_upload_bad_ext_rejected(client):
    r = _upload(client, name="evil.exe", data=b"MZ")
    assert r.status_code == 400


def test_upload_corrupt_content_rejected_67(client):
    """#67-②（0830 两机实锤）：扩展名合法但内容坏 → 当场 400 诚实拒收。

    此前上传成功只看扩展名+体积，坏文件静默落盘，到 AI 发图才在 pyrogram
    爆 decode 失败——报障人被「上传成功」骗了一整轮。
    """
    r = _upload(client, name="broken.jpg", data=b"\x00\x01broken-not-an-image")
    assert r.status_code == 400
    assert client.get("/api/personas/lin/media").json()["stats"]["total"] == 0
    r2 = _upload(client, name="broken.mp4", data=b"\x00\x01no-ftyp-here-at-all")
    assert r2.status_code == 400


def test_upload_cross_family_magic_allowed_67(client):
    """扩展名拍错但内容合法（.jpg 里装 PNG）→ 放行（接收端按内容解码）。"""
    r = _upload(client, name="mislabeled.jpg", data=_PNG + b"real-png-bytes")
    assert r.status_code == 200


def test_upload_too_large_rejected(client, monkeypatch):
    monkeypatch.setattr(pmr, "_MAX_PHOTO_BYTES", 10)
    r = _upload(client, data=b"0123456789ABCDEF")  # 16 > 10
    assert r.status_code == 413


def test_upload_unknown_persona_404(client):
    r = client.post(
        "/api/personas/ghost/media",
        files={"file": ("a.jpg", b"x", "application/octet-stream")})
    assert r.status_code == 404


def test_patch_updates_fields(client):
    mid = _upload(client, triggers="a").json()["item"]["id"]
    r = client.patch(f"/api/personas/lin/media/{mid}", json={
        "triggers": ["跳舞", "dance"], "enabled": False, "weight": 7,
        "caption_i18n": {"en": "dance"}})
    item = r.json()["item"]
    assert item["triggers"] == ["跳舞", "dance"] and item["enabled"] is False
    assert item["weight"] == 7 and item["caption_i18n"] == {"en": "dance"}


def test_patch_not_found_404(client):
    assert client.patch(
        "/api/personas/lin/media/nope", json={"caption": "x"}).status_code == 404


def test_delete_removes_row_and_file(client):
    item = _upload(client).json()["item"]
    fp = Path(item["file_path"])
    assert fp.is_file()
    r = client.delete(f"/api/personas/lin/media/{item['id']}")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not fp.exists()  # 文件也删了
    assert client.get("/api/personas/lin/media").json()["stats"]["total"] == 0


def test_delete_wrong_persona_404(client):
    mid = _upload(client).json()["item"]["id"]
    # 该条目属于 lin，用别的 persona 删 → 404（越权保护）
    PersonaManager.get_instance().upsert_profile("mia", {"name": "Mia"})
    assert client.delete(f"/api/personas/mia/media/{mid}").status_code == 404
    PersonaManager.get_instance().delete_profile("mia")


def test_audit_trail_upload_update_delete(audited_client):
    client, audit = audited_client
    mid = _upload(client, triggers="a").json()["item"]["id"]
    client.patch(f"/api/personas/lin/media/{mid}", json={"caption": "x"})
    client.delete(f"/api/personas/lin/media/{mid}")
    actions = [e[1] for e in audit.entries]
    assert actions == ["pmedia_upload", "pmedia_update", "pmedia_delete"]
    assert all(f"id={mid}" in e[2] for e in audit.entries)


def test_trigger_dry_run(client):
    _upload(client, triggers="跳舞")
    _upload(client, data=_PNG + b"generic-pool")  # 无触发词=通用池
    r = client.post("/api/personas/lin/media/test", json={"text": "给我跳舞看看"})
    body = r.json()
    assert body["pool"] == "keyword" and body["keyword_count"] == 1
    r2 = client.post("/api/personas/lin/media/test", json={"text": "在吗"})
    assert r2.json()["pool"] == "none"  # 非要图 + 无关键词 → 无候选


def test_metrics_exposes_persona_media(monkeypatch):
    from src.web.routes.drafts_routes import register_metrics_route
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    st = get_persona_media_store()
    r1 = st.add("lin", "photo", "/1", "/u1", caption="hi")
    st.add("lin", "video", "/2", "/u2")
    st.record_hit(r1["id"])
    st.record_hit(r1["id"])

    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    def _api_auth(r: Request):
        return True

    register_metrics_route(app, api_auth=_api_auth)
    c = TestClient(app, raise_server_exceptions=True)

    pm = c.get("/api/workspace/metrics").json().get("persona_media")
    assert pm is not None
    assert pm["total"] == 2 and pm["photo"] == 1 and pm["video"] == 1
    assert pm["total_hits"] == 2 and pm["top"][0]["id"] == r1["id"]

    txt = c.get("/api/workspace/metrics?format=prometheus").text
    assert "ws_persona_media_items 2" in txt
    assert "ws_persona_media_hits_total 2" in txt
    assert 'ws_persona_media_by_type{type="video"} 1' in txt
    reset_persona_media_store()


# ── Q-35 #316（4HK54G）：拒绝分支结构化 {ok:false, reason, detail} + 批量复现 ──────────

def _assert_reject(r, status, reason):
    assert r.status_code == status, (r.status_code, r.text)
    body = r.json()
    assert body["ok"] is False and body["reason"] == reason, body
    assert isinstance(body.get("detail"), str) and body["detail"], body  # 人话仍在，旧前端可读
    return body


def test_reject_ext_not_allowed_is_structured(client, monkeypatch):
    """iPhone HEIC 是「后端只见两张成功、其余零痕迹」最像的形状——现在有 reason + allowed。

    Q-40 B：后端有 pillow-heif 时 HEIC 会被转 JPEG 放行，这里钉住「不可用」分支
    （文案带 hint=export_jpeg，前端据此说「请在手机相册导出为 JPEG」）。
    """
    monkeypatch.setattr(pmr, "_heif_available", lambda: False)
    body = _assert_reject(_upload(client, name="IMG_0001.HEIC", data=b"\x00\x00\x00\x18ftypheic"),
                          400, "ext_not_allowed")
    assert body["ext"] == ".heic" and ".jpg" in body["allowed"]
    assert body["hint"] == "export_jpeg" and "JPEG" in body["detail"]


def test_reject_too_large_is_structured(client, monkeypatch):
    monkeypatch.setattr(pmr, "_MAX_PHOTO_BYTES", 10)
    body = _assert_reject(_upload(client, data=_PNG + b"0123456789ABCDEF"), 413, "too_large")
    assert body["limit_mb"] == 0 and body["bytes"] == len(_PNG) + 16 and body["media_type"] == "photo"


def test_reject_bad_content_is_structured(client):
    body = _assert_reject(_upload(client, name="broken.jpg", data=b"\x00\x01not-an-image"),
                          400, "bad_content")
    assert body["why"]


def test_reject_video_too_long_is_structured(client, monkeypatch):
    monkeypatch.setattr(pmr, "_MAX_VIDEO_DURATION_MS", 1000)
    monkeypatch.setattr(pmr, "_probe_video",
                        lambda p: {"duration_ms": 5000, "width": 1, "height": 1})
    body = _assert_reject(_upload(client, name="long.mp4", data=_MP4 + b"-dummy"), 413, "too_long")
    assert body["max_sec"] == 1 and body["duration_ms"] == 5000


def test_reject_persona_not_found_is_structured(client):
    r = client.post("/api/personas/ghost/media",
                    files={"file": ("a.jpg", _PNG + b"x", "application/octet-stream")})
    _assert_reject(r, 404, "persona_not_found")


def test_reject_empty_file_is_structured(client):
    _assert_reject(_upload(client, data=b""), 400, "empty_file")


def test_reject_paths_leave_a_server_log_line(client, caplog):
    """4HK54G：被拒的上传此前在服务端**零痕迹**（raise 在成功日志之前）。现在每条拒绝一行。"""
    import logging as _logging
    with caplog.at_level(_logging.INFO, logger="ai_chat_assistant.persona_media_routes"):
        _upload(client, name="x.heic", data=b"\x00")
    lines = [r.getMessage() for r in caplog.records if "上传拒绝" in r.getMessage()]
    assert lines and "reason=ext_not_allowed" in lines[0] and "name=x.heic" in lines[0]


class _FormatErrorTrap(__import__("logging").Handler):
    """把 logging 内部会吞掉的格式化错误变成断言：任何 record 都必须能 getMessage()。"""

    def __init__(self):
        super().__init__()
        self.errors = []

    def emit(self, record):
        try:
            record.getMessage()
        except Exception as ex:  # noqa: BLE001
            self.errors.append(f"{record.name}:{record.lineno} {record.msg!r} -> {ex}")


def test_batch_six_2mb_photos_all_land_without_logging_errors(client):
    """复现 #316 报障动作：一次选 ≥6 张 ~2MB 图逐个 POST（与 pmaUpload 串行同口径）。
    期望：6/6 200 + 6 条入库 + 全程零 logging 格式化错误（`--- Logging error ---` 形状）。"""
    import logging as _logging
    import os as _os
    trap = _FormatErrorTrap()
    root = _logging.getLogger()
    root.addHandler(trap)
    try:
        ok = 0
        for i in range(6):
            blob = _PNG + _os.urandom(2 * 1024 * 1024)  # 每张字节不同：sha 不去重
            r = _upload(client, name=f"IMG_{i:04d}.jpg", data=blob)
            assert r.status_code == 200, (i, r.status_code, r.text[:200])
            body = r.json()
            assert body["ok"] is True and body.get("deduped") is None
            ok += 1
    finally:
        root.removeHandler(trap)
    assert ok == 6
    assert client.get("/api/personas/lin/media").json()["stats"]["total"] == 6
    assert not trap.errors, trap.errors
