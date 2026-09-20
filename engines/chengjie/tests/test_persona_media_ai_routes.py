"""实施90 相册 AI 管线的路由/存量库门禁：
上传（EXIF 剥离 + pHash + 近重复预警 + 照片缩略图 + 打标排队）/ 补标端点 /
列表 AI 摘要 / 旧库三列迁移。"""
import io
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("PIL")

from PIL import Image

import src.web.routes.persona_media_routes as pmr
from src.companion.persona_media_store import (
    PersonaMediaStore,
    configure_persona_media_store,
    get_persona_media_store,
    reset_persona_media_store,
)
from src.utils.persona_manager import PersonaManager


def _jpeg_bytes(*, size=(320, 240), color=(180, 90, 60), exif_dt="",
                quality=90, seed=0) -> bytes:
    img = Image.new("RGB", size, color)
    if seed:
        px = img.load()
        for y in range(0, size[1], 3):
            for x in range(0, size[0], 3):
                px[x, y] = ((x * seed) % 255, (y * seed) % 255, (x + y) % 255)
    kw = {}
    if exif_dt:
        ex = Image.Exif()
        ex[0x9003] = exif_dt
        ex[0x0132] = exif_dt
        kw["exif"] = ex
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, **kw)
    return buf.getvalue()


class _CfgMgr:
    def __init__(self, config=None):
        self.config = config or {}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("lin", {"name": "Lin"})
    app = FastAPI()
    pmr.register_persona_media_routes(app, auth_dep=lambda: True,
                                      config_manager=_CfgMgr())
    yield TestClient(app), tmp_path / "albums"
    reset_persona_media_store()
    pm.delete_profile("lin")


def _upload(c, data, name="a.jpg"):
    return c.post("/api/personas/lin/media",
                  files={"file": (name, data, "application/octet-stream")})


def test_upload_strips_exif_makes_thumb_and_phash(client):
    c, albums = client
    raw = _jpeg_bytes(exif_dt="2026:01:15 10:00:00", seed=3)
    r = _upload(c, raw)
    assert r.status_code == 200
    item = r.json()["item"]
    assert len(item["phash"]) == 16
    assert item["thumb_url"].endswith(".thumb.webp")
    # #238 后 album_ai 默认开，但该夹具无 vision 端点 → 识图打不动：不排必败任务、条目留 untagged、原因回给前端
    assert item["tag_status"] == ""
    body = r.json()
    assert body["autotag"] is False and body["vision_ready"] is False and body["vision_reason"] == "no_endpoint"
    assert item["auto_meta"]["exif"]["month"] == 1
    assert item["auto_meta"]["exif"]["year"] == 2026
    # 落盘原图必须已剥 EXIF；缩略图文件真实存在
    saved = albums / "lin" / item["url"].rsplit("/", 1)[-1]
    with Image.open(saved) as im:
        assert len(dict(im.getexif())) == 0
    assert (albums / "lin" / item["thumb_url"].rsplit("/", 1)[-1]).is_file()


def test_upload_near_dup_warns_but_exact_dup_dedupes(client):
    c, _ = client
    base = _jpeg_bytes(seed=7, quality=95)
    r1 = _upload(c, base)
    id1 = r1.json()["item"]["id"]
    # 同图重编码（不同字节、同画面）→ near_dup 预警但照常入库
    r2 = _upload(c, _jpeg_bytes(seed=7, quality=55), name="b.jpg")
    body2 = r2.json()
    assert body2.get("deduped") is None
    assert body2["near_dup"]["id"] == id1
    assert body2["near_dup"]["distance"] <= 8
    # 完全同字节 → 旧语义 deduped 不变
    r3 = _upload(c, base, name="c.jpg")
    assert r3.json().get("deduped") is True


def test_list_ai_summary(client):
    c, _ = client
    _upload(c, _jpeg_bytes(seed=2))
    _upload(c, _jpeg_bytes(seed=9), name="b.jpg")
    d = c.get("/api/personas/lin/media").json()
    ai = d["ai"]
    assert ai["untagged"] == 2 and ai["tagged"] == 0
    assert ai["thumbs_missing"] == 0           # 上传即生成缩略图
    # #238 D-N3：默认开；该夹具无 vision 端点 → vision_ready=False + reason，前端据此明说而不是显示 0/N
    assert ai["enabled"] is True and ai["auto_on_upload"] is True
    assert ai["vision_ready"] is False and ai["vision_reason"] == "no_endpoint"
    assert "batch_active" in ai and "last_error" in ai


def test_upload_schedules_autotag_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("lin", {"name": "Lin"})
    calls = []
    monkeypatch.setattr(pmr._auto_tag, "schedule_tag",
                        lambda st, mid, vcfg, **kw: calls.append(mid) or True)
    # 识图就绪（探针不发网络；私网端点 + 客户端可建）→ 上传排队打标
    monkeypatch.setattr(pmr._auto_tag, "vision_probe", lambda vcfg: {"ready": True, "reason": ""})
    app = FastAPI()
    pmr.register_persona_media_routes(
        app, auth_dep=lambda: True,
        config_manager=_CfgMgr({"companion": {"selfie": {"album_ai": {
            "enabled": True}}}, "vision": {"base_urls": ["http://192.168.0.140:11434/v1"]}}))
    try:
        c = TestClient(app)
        r = _upload(c, _jpeg_bytes(seed=4))
        body = r.json()
        item = body["item"]
        assert item["tag_status"] == "pending"
        assert calls == [item["id"]]
        assert body["autotag"] is True and body["vision_ready"] is True
    finally:
        reset_persona_media_store()
        pm.delete_profile("lin")


def test_retag_all_and_single(client, monkeypatch):
    c, _ = client
    r = _upload(c, _jpeg_bytes(seed=5))
    mid = r.json()["item"]["id"]
    seen = {}

    def fake_batch(st, vcfg, *, persona_id=None, only_missing=True, limit=200,
                   face_ref="", **kw):
        seen.update(pid=persona_id, only_missing=only_missing, limit=limit,
                    face_ref=face_ref, publish_root=kw.get("publish_root", ""))
        return {"ok": True, "queued": 1, "assets": 0, "batch_active": True}

    monkeypatch.setattr(pmr._auto_tag, "run_batch", fake_batch)
    rr = c.post("/api/personas/lin/media/retag-all", json={})
    assert rr.status_code == 200 and rr.json()["queued"] == 1
    assert seen["pid"] == "lin" and seen["only_missing"] is True
    assert seen["face_ref"] == ""        # 该夹具无锁脸基准照
    assert seen["publish_root"]          # 发布根必须随批透传（裂图根因修复）

    sched = []
    monkeypatch.setattr(pmr._auto_tag, "schedule_tag",
                        lambda st, m, v, **kw: sched.append(m) or True)
    r1 = c.post(f"/api/personas/lin/media/{mid}/retag")
    assert r1.status_code == 200 and r1.json()["ok"] is True
    assert sched == [mid]
    assert r1.json()["item"]["tag_status"] == "pending"
    # 不存在的条目 404
    assert c.post("/api/personas/lin/media/nope/retag").status_code == 404


def test_media_test_route_carries_gate_context(client):
    c, _ = client
    _upload(c, _jpeg_bytes(seed=6))
    r = c.post("/api/personas/lin/media/test", json={"text": "来张照片"})
    assert r.status_code == 200
    body = r.json()
    assert "context" in body and "now_hour" in body["context"]
    assert body["conv_gates_included"] is False
    for cand in body["candidates"]:
        assert "blocked_by" in cand and "tags" in cand


def test_legacy_db_migration_adds_ai_columns(tmp_path):
    """旧库（无 phash/auto_meta/tag_status 三列）打开即幂等补列，旧行可读可升级。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE persona_media (
            id TEXT NOT NULL PRIMARY KEY,
            persona_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'photo',
            file_path TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            thumb_url TEXT NOT NULL DEFAULT '',
            triggers TEXT NOT NULL DEFAULT '[]',
            caption TEXT NOT NULL DEFAULT '',
            caption_i18n TEXT NOT NULL DEFAULT '{}',
            tags TEXT NOT NULL DEFAULT '[]',
            weight INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            tier TEXT NOT NULL DEFAULT '',
            min_bond_level INTEGER NOT NULL DEFAULT 0,
            bytes INTEGER NOT NULL DEFAULT 0,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT NOT NULL DEFAULT '',
            hits INTEGER NOT NULL DEFAULT 0,
            last_sent_at REAL NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );
        INSERT INTO persona_media (id, persona_id, media_type, url)
        VALUES ('old1', 'lin', 'photo', '/u/old1.jpg');
        """)
    conn.commit()
    conn.close()
    st = PersonaMediaStore(str(db))
    row = st.get("old1")
    assert row["phash"] == "" and row["auto_meta"] == {}
    assert row["tag_status"] == ""
    st.set_auto_tag("old1", phash="a" * 16, tag_status="tagged",
                    auto_meta={"desc": "旧图"})
    got = st.get("old1")
    assert got["phash"] == "a" * 16 and got["auto_meta"]["desc"] == "旧图"
    assert st.phashes("lin") == {"old1": "a" * 16}
    # 新增行走满列 INSERT 也正常
    new = st.add("lin", "photo", "", "/u/n.jpg", phash="b" * 16,
                 tag_status="pending")
    assert new["phash"] == "b" * 16 and new["tag_status"] == "pending"
