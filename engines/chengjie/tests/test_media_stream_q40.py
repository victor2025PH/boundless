# -*- coding: utf-8 -*-
"""Q-40 大文件媒体线门禁（#316 追加 / #322，2026-09-15）。

A 相册裸流：``media_stream.py`` 与 send-media 共用 / 25MB 裸流成功 / 26MB 413 带 size /
  multipart 旧路仍通 / ``/media/test`` 仍吃 2MB 闸 / 每条退出路径落 ``[pmedia] … reason= size= ms=``。
B 上限 25 / 100 / 5min 可配（``inbox.persona_media.limits`` 夹紧）/ HEIC 后端兜底 / 超长视频拒。
C 聊天视频回平台表：``DEFAULT_VIDEO_CAP_MB == 0``、LINE 100 / TG 200 / WA 64、messenger 仍 25；
  前端 ``_xhrSendMedia`` 超时随体积、附件条写「本会话最多 N MB」。
D ``personas.html``：``pmaDeleteItem`` 成功分支不再 ``pmaLoad``；灯箱不用 ``window.open`` /
  ``target=_blank``；「删除所选」按钮 + 词条齐平。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.web.routes.persona_media_routes as pmr
from src.companion.persona_media_store import (
    configure_persona_media_store, reset_persona_media_store)
from src.utils.persona_manager import PersonaManager
from src.web import media_stream as MS

_ROOT = Path(__file__).resolve().parents[1]
_PERSONAS = _ROOT / "src" / "web" / "templates" / "personas.html"
_INBOX = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_SEND_ROUTES = _ROOT / "src" / "web" / "routes" / "unified_inbox_send_routes.py"
_MB = 1024 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"
_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


def _png_bytes(size: int) -> bytes:
    return _PNG + b"\x00" * max(0, size - len(_PNG))


def _mp4_bytes(size: int) -> bytes:
    return _MP4 + b"\x00" * max(0, size - len(_MP4))


# ───────────────────────── A1. 共用模块本体 ─────────────────────────

async def _aiter(chunks):
    for c in chunks:
        yield c


def test_stream_to_file_writes_head_plus_rest_and_reports_size(tmp_path):
    it = _aiter([b"cd", b"ef", b"g"]).__aiter__()
    size, overflow = asyncio.run(MS.stream_to_file(it, str(tmp_path / "x.bin"), head=b"ab",
                                                   cap_bytes=100, write_buf=3))
    assert (size, overflow) == (7, False)
    assert (tmp_path / "x.bin").read_bytes() == b"abcdefg"


def test_stream_to_file_stops_at_cap_and_flags_overflow(tmp_path):
    async def _run():
        it = _aiter([b"1" * 10, b"2" * 10, b"3" * 10]).__aiter__()
        size, overflow = await MS.stream_to_file(it, str(tmp_path / "x.bin"), head=b"h" * 5,
                                                 cap_bytes=20)
        # 剩余块还能被 drain 吞掉（调用方拿它算 drained）
        drained = await MS.drain_stream(it, MS.DRAIN_MAX)
        return size, overflow, drained
    size, overflow, drained = asyncio.run(_run())
    assert overflow is True and 20 < size <= 25
    assert drained == 10


def test_stream_to_file_wraps_io_error_with_received_bytes(tmp_path):
    it = _aiter([b"x"]).__aiter__()
    with pytest.raises(MS.StreamRecvError) as ei:
        asyncio.run(MS.stream_to_file(it, str(tmp_path / "no_such_dir" / "x.bin"), head=b"h",
                                      cap_bytes=100))
    assert ei.value.received >= 0 and isinstance(ei.value.cause, OSError)


def test_read_head_reads_until_min_bytes_and_leaves_rest():
    async def _run():
        it = _aiter([b"a" * 4, b"b" * 4, b"c" * 4]).__aiter__()
        head = await MS.read_head(it, min_bytes=6)
        return head, await MS.drain_stream(it, 10 ** 9)
    head, rest = asyncio.run(_run())
    assert head == b"aaaabbbb" and rest == 4


def test_send_media_route_only_changed_import_and_shares_module():
    """send-media 抽共用模块：路由文件不再自带读流 / 写缓冲循环，只 import；旧名仍是别名。"""
    from src.web.routes import unified_inbox_send_routes as R
    assert R._drain_stream is MS.drain_stream
    assert R._SEND_MEDIA_DRAIN_MAX == MS.DRAIN_MAX and R._SEND_MEDIA_WRITE_BUF == MS.WRITE_BUF
    src = inspect.getsource(R._send_media_streamed)
    assert "_stream_to_file(" in src and "_read_head(" in src
    assert 'open, local, "wb"' not in src, "写盘循环应只在 media_stream.stream_to_file 一处"
    # 相册路由也吃同一份
    psrc = inspect.getsource(pmr)
    assert "from src.web.media_stream import" in psrc and "stream_to_file(" in psrc


# ───────────────────────── A2/B. 相册上传口 ─────────────────────────

class _CfgMgr:
    def __init__(self, config=None):
        self.config = config or {}


@pytest.fixture()
def album(tmp_path, monkeypatch):
    monkeypatch.setattr(pmr, "_ALBUM_ROOT", tmp_path / "albums")
    monkeypatch.setattr(pmr, "_probe_video", lambda p: {"duration_ms": 3000, "width": 1, "height": 1})
    monkeypatch.setattr(pmr, "_make_video_thumbnail", lambda *a, **k: False)
    reset_persona_media_store()
    configure_persona_media_store(":memory:")
    pm = PersonaManager.get_instance()
    pm.upsert_profile("lin", {"name": "Lin"})
    cm = _CfgMgr()
    app = FastAPI()
    pmr.register_persona_media_routes(app, auth_dep=lambda: True, config_manager=cm)
    yield TestClient(app), cm, tmp_path / "albums"
    reset_persona_media_store()
    pm.delete_profile("lin")


def _stream_post(client, name, data, mime="application/octet-stream", **q):
    from urllib.parse import urlencode
    qs = urlencode(dict(name=name, **q))
    return client.post(f"/api/personas/lin/media?{qs}", content=data, headers={"Content-Type": mime})


def test_media_caps_endpoint_exposes_limits_and_stream_flag(album):
    client, cm, _ = album
    d = client.get("/api/personas/media-caps").json()
    assert d["album_stream_upload"] is True
    assert d["photo_max_mb"] == 25 and d["video_max_mb"] == 100 and d["video_max_sec"] == 300
    assert ".jpg" in d["image_exts"] and ".mp4" in d["video_exts"]
    assert isinstance(d["heic_backend"], bool)
    cm.config = {"inbox": {"persona_media": {"limits": {"photo_mb": 40, "video_mb": 9999, "video_max_sec": 60}}}}
    d = client.get("/api/personas/media-caps").json()
    assert d["photo_max_mb"] == 40 and d["video_max_mb"] == 2048 and d["video_max_sec"] == 60


def test_stream_upload_25mb_photo_lands_with_query_metadata(album, caplog):
    client, _, root = album
    data = _png_bytes(25 * _MB)
    with caplog.at_level(logging.INFO, logger="ai_chat_assistant.persona_media_routes"):
        r = _stream_post(client, "big.png", data, "image/png", triggers="风景,海边", caption="海")
    assert r.status_code == 200, r.text[:300]
    item = r.json()["item"]
    assert item["media_type"] == "photo" and item["bytes"] == len(data)
    assert item["triggers"] == ["风景", "海边"] and item["caption"] == "海"
    fp = Path(item["file_path"])
    assert fp.is_file() and fp.stat().st_size == len(data) and fp.read_bytes()[:8] == _PNG
    # 临时 .part 不残留（相册不存两份）
    assert not list((root / "lin").glob(".up_*.part"))
    ok_lines = [rec.getMessage() for rec in caplog.records if "reason=ok" in rec.getMessage()]
    assert ok_lines and "mode=stream" in ok_lines[0] and "size=" in ok_lines[0] and "ms=" in ok_lines[0]


def test_stream_upload_26mb_photo_is_413_with_size_before_reading_body(album, caplog):
    client, _, root = album
    size = 26 * _MB
    with caplog.at_level(logging.INFO, logger="ai_chat_assistant.persona_media_routes"):
        r = _stream_post(client, "huge.png", _png_bytes(size), "image/png")
    assert r.status_code == 413, r.text[:300]
    body = r.json()
    assert body["ok"] is False and body["reason"] == "too_large"
    assert body["limit_mb"] == 25 and body["bytes"] == size and body["media_type"] == "photo"
    assert body["over_mb"] == 1.0
    assert "25" in body["detail"]
    assert not (root / "lin").exists() or not list((root / "lin").iterdir())
    line = [rec.getMessage() for rec in caplog.records if "reason=too_large" in rec.getMessage()]
    assert line and f"size={size}" in line[0] and "ms=" in line[0] and "mode=stream" in line[0]


def test_stream_upload_without_content_length_hits_streaming_gate(album):
    """没有 Content-Length（chunked）→ 流式闸中途停：仍 413，bytes 为已收（≥上限）。"""
    client, _, root = album

    def _gen():
        for _ in range(27):
            yield b"\x00" * _MB

    r = client.post("/api/personas/lin/media?name=chunked.png", content=_gen(),
                    headers={"Content-Type": "image/png"})
    assert r.status_code == 413
    assert r.json()["bytes"] > 25 * _MB
    assert not list((root / "lin").glob("*")) if (root / "lin").exists() else True


def test_multipart_legacy_path_still_works_and_dedups_with_stream(album):
    client, _, _ = album
    data = _png_bytes(3 * _MB)
    r1 = client.post("/api/personas/lin/media", files={"file": ("a.png", data, "image/png")},
                     data={"triggers": "自拍"})
    assert r1.status_code == 200 and r1.json()["item"]["triggers"] == ["自拍"]
    r2 = _stream_post(client, "a-again.png", data, "image/png")
    assert r2.status_code == 200 and r2.json().get("deduped") is True
    assert r2.json()["item"]["id"] == r1.json()["item"]["id"]


def test_stream_upload_video_100mb_ok_101mb_413(album, monkeypatch):
    client, _, _ = album
    # 走真流式（100MB 全量进测试太慢）：把上限压到 4MB 验同一条路
    monkeypatch.setattr(pmr, "_MAX_VIDEO_BYTES", 4 * _MB)
    ok = _stream_post(client, "clip.mp4", _mp4_bytes(4 * _MB), "video/mp4")
    assert ok.status_code == 200 and ok.json()["item"]["media_type"] == "video"
    assert Path(ok.json()["item"]["file_path"]).stat().st_size == 4 * _MB
    bad = _stream_post(client, "clip2.mp4", _mp4_bytes(4 * _MB + 1), "video/mp4")
    assert bad.status_code == 413 and bad.json()["media_type"] == "video" and bad.json()["limit_mb"] == 4


def test_video_over_configured_duration_is_rejected_and_file_removed(album, monkeypatch):
    client, cm, root = album
    cm.config = {"inbox": {"persona_media": {"limits": {"video_max_sec": 2}}}}
    monkeypatch.setattr(pmr, "_probe_video", lambda p: {"duration_ms": 5000, "width": 1, "height": 1})
    r = _stream_post(client, "long.mp4", _mp4_bytes(_MB), "video/mp4")
    assert r.status_code == 413 and r.json()["reason"] == "too_long" and r.json()["max_sec"] == 2
    assert not list((root / "lin").glob("*.mp4"))


def test_stream_upload_bad_magic_and_empty_and_missing_name(album):
    client, _, _ = album
    r = _stream_post(client, "x.png", b"\x00" * 4096, "image/png")
    assert r.status_code == 400 and r.json()["reason"] == "bad_content"
    r = _stream_post(client, "e.png", b"", "image/png")
    assert r.status_code == 400 and r.json()["reason"] == "empty_file"
    r = client.post("/api/personas/lin/media", content=b"\x89PNGxx", headers={"Content-Type": "image/png"})
    assert r.status_code == 400 and r.json()["reason"] == "file_required"


def test_heic_without_backend_is_ext_not_allowed_with_export_hint(album, monkeypatch):
    client, _, _ = album
    monkeypatch.setattr(pmr, "_heif_available", lambda: False)
    r = _stream_post(client, "IMG_1.HEIC", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64, "image/heic")
    assert r.status_code == 400
    d = r.json()
    assert d["reason"] == "ext_not_allowed" and d["hint"] == "export_jpeg" and "JPEG" in d["detail"]
    caps = client.get("/api/personas/media-caps").json()
    assert caps["heic_backend"] is False


def test_heic_with_backend_is_converted_to_jpeg_single_copy(album, monkeypatch):
    """pillow-heif 可用（这里用假解码器）→ 落库一份 .jpg，扩展名 / magic / 尺寸都是 JPEG 的。"""
    client, _, root = album
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 2048 + b"\xff\xd9"
    monkeypatch.setattr(pmr, "_heif_available", lambda: True)
    monkeypatch.setattr(pmr, "_heic_to_jpeg", lambda data, quality=90: jpeg)
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * (3 * _MB)
    r = _stream_post(client, "IMG_2.heic", heic, "image/heic")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["converted"] == "heic->jpeg"
    item = d["item"]
    assert item["file_path"].endswith(".jpg") and item["bytes"] == len(jpeg)
    assert Path(item["file_path"]).read_bytes()[:3] == b"\xff\xd8\xff"
    files = list((root / "lin").iterdir())
    assert len([f for f in files if f.suffix == ".jpg"]) == 1 and not any(f.suffix == ".heic" for f in files)
    # 解码失败 → bad_content，不留半成品
    monkeypatch.setattr(pmr, "_heic_to_jpeg", lambda data, quality=90: b"")
    r2 = _stream_post(client, "IMG_3.heic", heic + b"\x01", "image/heic")
    assert r2.status_code == 400 and r2.json()["reason"] == "bad_content"
    assert not list((root / "lin").glob(".up_*.part"))


@pytest.mark.skipif(not pmr._heif_available(), reason="pillow-heif 未安装：真解码只在装了可选依赖的机器上验")
def test_heic_real_decode_when_pillow_heif_installed(tmp_path):
    import pillow_heif  # type: ignore
    from PIL import Image
    im = Image.new("RGB", (64, 48), (200, 30, 30))
    buf = tmp_path / "a.heic"
    pillow_heif.register_heif_opener()
    im.save(buf, format="HEIF")
    out = pmr._heic_to_jpeg(buf.read_bytes())
    assert out[:3] == b"\xff\xd8\xff"


def test_limits_resolver_clamps_and_defaults():
    lim = pmr.resolve_persona_media_limits(None)
    assert (lim["photo_mb"], lim["video_mb"], lim["video_max_sec"]) == (25, 100, 300)
    lim = pmr.resolve_persona_media_limits({"inbox": {"persona_media": {"limits": {
        "photo_mb": 0, "video_mb": "abc", "video_max_sec": -5}}}})
    assert lim["photo_mb"] == 1 and lim["video_mb"] == 100 and lim["video_max_sec"] == 1
    lim = pmr.resolve_persona_media_limits({"inbox": {"persona_media": {"limits": "junk"}}})
    assert lim["photo_bytes"] == 25 * _MB


def test_media_test_json_route_not_exempt_from_body_gate():
    """裸流分支只在上传口；``/media/test`` 等 JSON 口仍吃 2MB 闸（matcher 契约，与 Q-35 D 同）。"""
    from src.web.admin import is_persona_album_upload_path
    assert is_persona_album_upload_path("/api/personas/lin/media")
    assert not is_persona_album_upload_path("/api/personas/lin/media/test")
    assert not is_persona_album_upload_path("/api/personas/media-caps")


# ───────────────────────── A3. 前端：pmaUpload 裸流 + 进度 ─────────────────────────

def _fn_src(src: str, name: str) -> str:
    i = src.find(f"function {name}(")
    assert i > 0, name
    j = src.find("\nfunction ", i + 1)
    k = src.find("\nasync function ", i + 1)
    ends = [x for x in (j, k) if x > 0]
    return src[i:min(ends)] if ends else src[i:]


def test_personas_pma_upload_uses_xhr_stream_with_progress_and_caps():
    src = _PERSONAS.read_text(encoding="utf-8")
    up = _fn_src(src, "pmaUpload")
    assert "_pmaLoadCaps(" in up and "album_stream_upload" in up
    assert "_pmaXhrUpload(" in up and "_pmaPrepareFile(" in up
    assert "new FormData()" in up, "旧后端（无 caps）仍走 multipart"
    xhr = _fn_src(src, "_pmaXhrUpload")
    assert "upload.onprogress" in xhr and "/media?" in xhr and "xhr.send(file)" in xhr
    assert "Math.max(300000" in xhr, "超时随体积 max(300s, MB×3s)"
    assert "/api/personas/media-caps" in src
    # 结果条进度行
    assert "pma-upr-live" in src and "_pmaUpLive(" in up
    # 每条退出 console 落 reason / size / ms
    assert "reason=" in up and "size=" in up and "ms=" in up


def test_personas_prepare_file_heic_and_shrink_rules():
    src = _PERSONAS.read_text(encoding="utf-8")
    prep = _fn_src(src, "_pmaPrepareFile")
    assert "_pmaIsVideo(f)" in prep and "return out" in prep, "视频不压"
    assert "_PMA_IMG_MAX_EDGE" in src and "4096" in src and "8 * 1048576" in src
    assert "0.85" in prep and "0.9" in prep
    assert "image/png" in prep and "isPng" in prep, "PNG 透明保留 PNG"
    assert "heic_client_undecodable" in prep
    # 失败文案：HEIC 写明导出 JPEG；太大带 size / over
    fr = _fn_src(src, "_pmaFailReasonText")
    assert "pma_fr_heic" in fr and "pma_fr_too_large_detail" in fr


# ───────────────────────── C. 聊天视频回平台表 ─────────────────────────

def test_chat_video_cap_back_to_platform_table_messenger_still_25():
    from src.inbox import media_limits as ML
    assert ML.DEFAULT_VIDEO_CAP_MB == 0
    assert "messenger" not in ML.PLATFORM_MEDIA_CAP_MB
    assert ML.media_cap_mb_for(None, "line", "video") == 100
    assert ML.media_cap_mb_for(None, "telegram", "video") == 200
    assert ML.media_cap_mb_for(None, "whatsapp", "video") == 64
    assert ML.media_cap_mb_for(None, "messenger", "video") == 25
    assert ML.media_cap_mb_for(None, "messenger", "image") == 25
    # 运营显式压顶仍生效
    assert ML.media_cap_mb_for({"inbox": {"media": {"video_limit_mb": 30}}}, "telegram", "video") == 30


def test_inbox_only_three_functions_touched_and_timeout_scales_with_size():
    src = _INBOX.read_text(encoding="utf-8")
    xhr = _fn_src(src, "_xhrSendMedia")
    assert "Math.max(300000" in xhr and "1048576)*3000" in xhr.replace(" ", "")
    assert "xhr.timeout=300000;" not in xhr
    item = _fn_src(src, "_mediaItemHtml")
    assert "inbox.media.cap_hint" in item and "_mediaCapMb(it.file)" in item
    assert "data-mpi-shrink" in item and "_mediaShrinkItem(" in item
    stage = _fn_src(src, "_stageMedia")
    assert "inbox.media.too_large_over" in stage and "canShrink" in stage and "8*1048576" in stage
    shrink = _fn_src(src, "_mediaShrinkItem")
    assert "2560" in shrink and "0.82" in shrink and "it.shrunk=true" in shrink
    assert "window._mediaShrinkItem=_mediaShrinkItem" in src


def test_inbox_media_words_present_and_placeholders_match():
    from src.web.i18n_packs import inbox_workspace as W
    for k in ("inbox.media.cap_hint", "inbox.media.too_large_over", "inbox.media.shrink_send",
              "inbox.media.shrink_send_title", "inbox.media.shrunk", "inbox.media.shrink_fail"):
        assert k in W.ZH and k in W.EN, k
        assert set(re.findall(r"\{(\w+)\}", W.ZH[k])) == set(re.findall(r"\{(\w+)\}", W.EN[k])), k
    assert "{over}" in W.ZH["inbox.media.too_large_over"] and "{plat}" in W.ZH["inbox.media.cap_hint"]


# ───────────────────────── D. #322 相册 UX ─────────────────────────

def test_personas_delete_item_success_branch_does_not_reload():
    src = _PERSONAS.read_text(encoding="utf-8")
    fn = _fn_src(src, "pmaDeleteItem")
    i = fn.find("if (r.ok")
    ok_branch = fn[i:fn.find("return;", i)]
    assert "_pmaRemoveCards(" in ok_branch and "pmaLoad(" not in ok_branch
    assert "_pmaReloadKeepScroll(" in fn
    keep = _fn_src(src, "_pmaReloadKeepScroll")
    assert "scrollTop" in keep and "pmaLoad(" in keep
    rm = _fn_src(src, "_pmaRemoveCards")
    assert "card.remove()" in rm and "_pmaRefreshSummary(" in rm and "_alSel[" in rm


def test_personas_lightbox_in_page_not_window_open():
    src = _PERSONAS.read_text(encoding="utf-8")
    card = _fn_src(src, "_pmaCard")
    assert 'target="_blank"' not in card and "_pmaLightbox(" in card
    lb = _fn_src(src, "_pmaLightbox")
    assert "window.open" not in lb and "pma-lightbox" in lb
    assert "_pmaLightboxClose" in src and "'Escape'" in src
    # Esc 在捕获相位截住，不落到 closeProfileEditor
    i = src.find("if (!document.getElementById('pma-lightbox')) return;")
    assert i > 0 and "stopPropagation" in src[i:i + 200] and "}, true);" in src[i:i + 400]
    assert ".pma-lightbox{" in src and "--th-bg-scrim60" in src


def test_personas_delete_selected_button_and_words():
    src = _PERSONAS.read_text(encoding="utf-8")
    assert 'onclick="_alDeleteSelected()"' in src and "window._alDeleteSelected" in src
    fn = _fn_src(src, "_alDeleteSelected")
    assert "al_del_sel_confirm" in fn and "_pmaRemoveCards(okIds)" in fn
    from src.web.i18n_packs import persona_apply_modal as P
    for k in ("al_del_sel", "al_del_sel_confirm", "al_del_sel_ok", "al_del_sel_fail", "pma_lb_close",
              "pma_up_live_prep", "pma_up_live_server", "pma_fr_too_large_detail", "pma_fr_heic"):
        assert k in P.ZH and k in P.EN, k
        assert set(re.findall(r"\{(\w+)\}", P.ZH[k])) == set(re.findall(r"\{(\w+)\}", P.EN[k])), k


def test_errors_stock_has_heic_export_hint_key():
    from src.web.i18n_packs import errors_stock as E
    assert "err.pmedia.heic_export_jpeg" in E.ZH and "err.pmedia.heic_export_jpeg" in E.EN
    assert "JPEG" in E.ZH["err.pmedia.heic_export_jpeg"] and "{ext}" in E.EN["err.pmedia.heic_export_jpeg"]
