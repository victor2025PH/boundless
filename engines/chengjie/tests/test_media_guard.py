"""出站媒体内容守卫 + 显式下载端点 门禁（P0 2026-08-17）。

覆盖：
- magic bytes 嗅探（图/音/视/文档/可执行体/未知）；
- validate_outbound_media 三类拒绝语义（ext_forbidden / executable_content /
  magic_mismatch）与不该误拦的边界（txt/docx/跨格式同类图片）；
- 守卫扩展名三表与 protocol_bridge._OUT_* 同口径（注释里承诺的同步门禁）；
- 下载文件名消毒 + realpath 路径穿越守卫；
- /api/unified-inbox/media-download 端点端到端（attachment 头/穿越拒绝/404）。
"""

from __future__ import annotations

import os

import pytest

from src.inbox.media_guard import (
    DANGEROUS_EXTS,
    resolve_contained_path,
    safe_download_name,
    sniff_media_kind,
    validate_outbound_media,
)

# ── 嗅探 ─────────────────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8
OGG = b"OggS" + b"\x00" * 16
MP3_ID3 = b"ID3\x03" + b"\x00" * 16
WAV = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 16
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8
M4A = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 8
PDF = b"%PDF-1.7" + b"\x00" * 16
ZIP = b"PK\x03\x04" + b"\x00" * 16
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8
EXE = b"MZ\x90\x00" + b"\x00" * 16
ELF = b"\x7fELF\x02" + b"\x00" * 16


@pytest.mark.parametrize("data,kind", [
    (PNG, "image"), (JPEG, "image"), (GIF, "image"), (WEBP, "image"),
    (OGG, "audio"), (MP3_ID3, "audio"), (WAV, "audio"),
    (b"\xff\xfbxx" + b"\x00" * 8, "audio"),   # mp3 裸帧
    (b"#!AMR\n" + b"\x00" * 8, "audio"),
    (WEBM, "video"), (MP4, "mp4"), (M4A, "audio"),
    (PDF, "pdf"), (ZIP, "zip"), (OLE, "ole"),
    (EXE, "executable"), (ELF, "executable"),
    (b"hello plain text", ""), (b"", ""),
])
def test_sniff_media_kind(data, kind):
    assert sniff_media_kind(data) == kind


# ── 验证语义 ─────────────────────────────────────────────────────────

def test_dangerous_ext_rejected_case_insensitive():
    for name in ("evil.exe", "Evil.EXE", "run.Bat", "x.PS1", "a.apk"):
        v = validate_outbound_media(name, PNG)  # 内容再无害也拒
        assert not v["ok"] and v["reason"] == "ext_forbidden", name


def test_executable_content_rejected_any_extension():
    # .exe 改名 .jpg / .pdf / .docx——伪装件全拒
    for name in ("photo.jpg", "report.pdf", "notes.docx"):
        v = validate_outbound_media(name, EXE)
        assert not v["ok"] and v["reason"] == "executable_content", name
    v = validate_outbound_media("lib.bin", ELF)
    assert not v["ok"] and v["reason"] == "executable_content"


def test_media_category_requires_matching_magic():
    # 文本冒充图片/随机字节冒充音频 → 拒
    assert validate_outbound_media("a.png", b"not an image")["reason"] == \
        "magic_mismatch"
    assert validate_outbound_media("a.mp3", PNG)["reason"] == "magic_mismatch"
    assert validate_outbound_media("a.mp4", OGG)["reason"] == "magic_mismatch"


def test_cross_format_same_category_allowed():
    # png 内容存成 .jpg（截图工具常见）：同属 image 类，放行不误拦
    assert validate_outbound_media("shot.jpg", PNG)["ok"]
    # m4a 挂 mp4-family brand / opus 是 ogg 容器
    assert validate_outbound_media("memo.m4a", MP4)["ok"]
    assert validate_outbound_media("memo.opus", OGG)["ok"]


def test_document_catchall_is_lenient():
    # txt/csv 无魔数、docx=zip、pdf——document 大类只拒可执行体
    assert validate_outbound_media("notes.txt", b"hello world")["ok"]
    assert validate_outbound_media("sheet.csv", b"a,b,c\n1,2,3")["ok"]
    assert validate_outbound_media("doc.docx", ZIP)["ok"]
    assert validate_outbound_media("doc.pdf", PDF)["ok"]
    assert validate_outbound_media("old.xls", OLE)["ok"]


def test_ext_tables_synced_with_protocol_bridge():
    """守卫的扩展名三表必须与 protocol_bridge 出站归类同口径（注释承诺）。"""
    from src.inbox import media_guard as mg
    from src.integrations import protocol_bridge as pb
    assert mg._IMAGE_EXTS == frozenset(pb._OUT_IMAGE_EXT)
    assert mg._AUDIO_EXTS == frozenset(pb._OUT_AUDIO_EXT)
    assert mg._VIDEO_EXTS == frozenset(pb._OUT_VIDEO_EXT)
    # 大类归属逐扩展名一致
    for e in mg._IMAGE_EXTS | mg._AUDIO_EXTS | mg._VIDEO_EXTS | {".pdf", ".xyz"}:
        assert mg._ext_category(e) == pb.media_type_from_ext(e), e


def test_dangerous_exts_never_overlap_media_exts():
    from src.inbox import media_guard as mg
    assert not (DANGEROUS_EXTS & (mg._IMAGE_EXTS | mg._AUDIO_EXTS
                                  | mg._VIDEO_EXTS))


# ── 下载名消毒 / 路径穿越 ───────────────────────────────────────────

def test_safe_download_name():
    assert safe_download_name("报价单.pdf") == "报价单.pdf"
    assert safe_download_name("") == "download.bin"
    assert safe_download_name("   ..  ") == "download.bin"
    # 分隔符/控制字符全被清掉，产物不含任何路径成分
    out = safe_download_name("..\\..\\evil\x00name.txt")
    assert "\\" not in out and "/" not in out and "\x00" not in out
    out2 = safe_download_name("../../etc/passwd")
    assert "/" not in out2
    # 超长截断保扩展名
    long = safe_download_name("x" * 300 + ".pdf")
    assert len(long) <= 150 and long.endswith(".pdf")


def test_resolve_contained_path(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    inside = root / "tg" / "a.jpg"
    inside.parent.mkdir()
    inside.write_bytes(b"x")
    got = resolve_contained_path(str(root), str(inside))
    assert got and os.path.samefile(got, inside)
    # ../ 穿越 → None
    assert resolve_contained_path(
        str(root), str(root / ".." / "secret.txt")) is None
    assert resolve_contained_path(str(root), "") is None
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"y")
    assert resolve_contained_path(str(root), str(outside)) is None


# ── 下载端点端到端 ───────────────────────────────────────────────────

def _download_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.integrations import protocol_bridge as pb
    from src.web.routes.unified_inbox_send_routes import register_send_routes

    root = tmp_path / "protocol_media"
    (root / "telegram").mkdir(parents=True)
    (root / "telegram" / "out_a_abc123.pdf").write_bytes(b"%PDF-1.7 test")
    monkeypatch.setattr(pb, "protocol_media_root", lambda: root)

    app = FastAPI()
    register_send_routes(app, api_auth=lambda: None, page_auth=lambda: None)
    return TestClient(app)


def test_media_download_endpoint_attachment(tmp_path, monkeypatch):
    c = _download_client(tmp_path, monkeypatch)
    r = c.get("/api/unified-inbox/media-download", params={
        "ref": "/static/protocol_media/telegram/out_a_abc123.pdf",
        "name": "报价单.pdf"})
    assert r.status_code == 200, r.text
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert r.content == b"%PDF-1.7 test"


def test_media_download_rejects_traversal_and_foreign_refs(tmp_path,
                                                           monkeypatch):
    c = _download_client(tmp_path, monkeypatch)
    # ../ 穿越
    r = c.get("/api/unified-inbox/media-download", params={
        "ref": "/static/protocol_media/telegram/../../secret.txt"})
    assert r.status_code == 404
    # 非 protocol_media 前缀
    r2 = c.get("/api/unified-inbox/media-download", params={
        "ref": "/static/css/site.css"})
    assert r2.status_code == 404
    # 不存在的文件
    r3 = c.get("/api/unified-inbox/media-download", params={
        "ref": "/static/protocol_media/telegram/nope.pdf"})
    assert r3.status_code == 404


def test_send_media_route_wires_guard():
    """send-media 路由必须调用 validate_outbound_media（静态接线断言，
    防未来重构把守卫掉线）。"""
    import inspect
    from src.web.routes import unified_inbox_send_routes as m
    src = inspect.getsource(m)
    assert "validate_outbound_media" in src
    assert "err.inbox.media_ext_forbidden" in src
    assert "err.inbox.media_magic_mismatch" in src
