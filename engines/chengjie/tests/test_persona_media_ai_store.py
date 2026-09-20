"""store AI 三列（phash/auto_meta/tag_status）迁移与专用写点门禁（实施90）。

重点：**旧库**（迁移前 24 列 schema）打开即自动补列且旧行读出形态正确——
生产 persona_media.db 就是这形态，迁移红了=升级当晚全量打标链路瘫。
"""
import sqlite3

from src.companion.persona_media_store import PersonaMediaStore

_LEGACY_DDL = """
CREATE TABLE persona_media (
    id             TEXT NOT NULL PRIMARY KEY,
    persona_id     TEXT NOT NULL,
    media_type     TEXT NOT NULL DEFAULT 'photo',
    file_path      TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    thumb_url      TEXT NOT NULL DEFAULT '',
    triggers       TEXT NOT NULL DEFAULT '[]',
    caption        TEXT NOT NULL DEFAULT '',
    caption_i18n   TEXT NOT NULL DEFAULT '{}',
    tags           TEXT NOT NULL DEFAULT '[]',
    weight         INTEGER NOT NULL DEFAULT 1,
    enabled        INTEGER NOT NULL DEFAULT 1,
    tier           TEXT NOT NULL DEFAULT '',
    min_bond_level INTEGER NOT NULL DEFAULT 0,
    bytes          INTEGER NOT NULL DEFAULT 0,
    width          INTEGER NOT NULL DEFAULT 0,
    height         INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    sha256         TEXT NOT NULL DEFAULT '',
    hits           INTEGER NOT NULL DEFAULT 0,
    last_sent_at   REAL NOT NULL DEFAULT 0,
    created_by     TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL DEFAULT 0
);
CREATE TABLE persona_media_sends (
    conv_key    TEXT NOT NULL,
    media_id    TEXT NOT NULL,
    persona_id  TEXT NOT NULL DEFAULT '',
    series      TEXT NOT NULL DEFAULT '',
    sent_at     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (conv_key, media_id)
);
"""


def _make_legacy_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_LEGACY_DDL)
    conn.execute(
        "INSERT INTO persona_media (id, persona_id, media_type, file_path, url,"
        " sha256, created_at, updated_at) VALUES"
        " ('m1', 'lin', 'photo', '/x/a.jpg', '/static/a.jpg', 's1', 1, 1)")
    conn.commit()
    conn.close()


def test_legacy_db_migrates_and_reads(tmp_path):
    db = tmp_path / "legacy.db"
    _make_legacy_db(db)
    st = PersonaMediaStore(str(db))
    row = st.get("m1")
    assert row is not None
    assert row["phash"] == "" and row["auto_meta"] == {} and row["tag_status"] == ""
    # 旧库上新写点直接可用
    st.set_auto_tag("m1", phash="a" * 16,
                    auto_meta={"desc": "x", "exif": {"month": 2}},
                    tag_status="tagged", tags=["scene:beach"],
                    thumb_url="/static/a.jpg.thumb.webp")
    got = st.get("m1")
    assert got["phash"] == "a" * 16
    assert got["auto_meta"]["exif"]["month"] == 2
    assert got["tag_status"] == "tagged"
    assert got["tags"] == ["scene:beach"]
    assert got["thumb_url"].endswith(".thumb.webp")
    assert st.phashes("lin") == {"m1": "a" * 16}


def test_set_auto_tag_partial_and_noop():
    st = PersonaMediaStore(":memory:")
    row = st.add("lin", "photo", "/x/a.jpg", "/static/a.jpg")
    mid = row["id"]
    # 全 None＝纯读；不动任何字段
    same = st.set_auto_tag(mid)
    assert same["updated_at"] == row["updated_at"]
    st.set_auto_tag(mid, phash="b" * 16)
    got = st.get(mid)
    assert got["phash"] == "b" * 16 and got["auto_meta"] == {}
    # 只改 status 不动 phash
    st.set_auto_tag(mid, tag_status="failed")
    got2 = st.get(mid)
    assert got2["phash"] == "b" * 16 and got2["tag_status"] == "failed"


def test_add_accepts_phash_and_status():
    st = PersonaMediaStore(":memory:")
    row = st.add("lin", "photo", "/x/b.jpg", "/static/b.jpg",
                 phash="c" * 16, tag_status="pending")
    assert row["phash"] == "c" * 16 and row["tag_status"] == "pending"
    assert st.phashes("lin") == {row["id"]: "c" * 16}
    # 视频/无指纹条目不进 phashes 映射
    st.add("lin", "video", "/x/v.mp4", "/static/v.mp4")
    assert len(st.phashes("lin")) == 1
