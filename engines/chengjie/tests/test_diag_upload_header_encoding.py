# -*- coding: utf-8 -*-
"""报障链「x-diag-meta 头必须 latin-1 可编码」门禁（2026-09-15 事故沉淀）。

事故：人设页红条「页面脚本出错（_psnArRender）⤴ 报障」直达报障 → note 含中文 →
``json.dumps(..., ensure_ascii=False)`` 放进 HTTP 头 → ``http.client.putheader`` 在
**一个字节都没发出去之前**抛 ``UnicodeEncodeError`` → ``classify_upload_error`` 折成
``upstream_unreachable`` → 重试 / mini / 暂存 / 连通自诊整条链误诊「网络掐断」，用户被
告知「检查网络」。中文产品的主路径 100% 失败，3 天零告警。

既有门禁全部 mock 了 ``urllib.request.urlopen``——头编码发生在 mock **之内**，永远测不到。
本文件两层补齐：
① 纯函数：``diag_meta_header`` 出口恒 ASCII 且 JSON 可还原；``classify_upload_error``
   把本地确定性错误分到 ``local_error``；
② **真 http.client**：起一个 127.0.0.1 HTTP 服务当官网，``build_and_upload`` 带中文 note
   真发——服务端收到的头必须能 ``json.loads`` 还原出同一段中文。
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from src.utils import diag_upload as du
from src.utils.diag_upload import (
    build_and_upload,
    classify_upload_error,
    diag_meta_header,
    flush_diag_outbox,
    is_local_error,
    list_staged,
    stage_bundle,
)

CJK_NOTE = "/personas | 页面脚本出错，部分功能可能失效（_psnArRender）"


class _CM:
    def __init__(self, root: Path):
        cfg_dir = root / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = str(cfg_dir / "config.yaml")
        self.config = {}


# ── ① 纯函数 ────────────────────────────────────────────────────────────────────

def test_diag_meta_header_is_latin1_safe_and_roundtrips():
    hdr = diag_meta_header({"app": "1.0.85", "fp": "D316-8D51", "note": CJK_NOTE})
    hdr.encode("latin-1")            # HTTP 头契约：必过
    assert hdr.isascii()
    assert json.loads(hdr)["note"] == CJK_NOTE


def test_diag_meta_header_handles_emoji_and_arrows():
    hdr = diag_meta_header({"note": "⤴ 报障 🔧 ñ"})
    hdr.encode("latin-1")
    assert json.loads(hdr)["note"] == "⤴ 报障 🔧 ñ"


def test_classify_unicode_encode_error_is_local_error():
    ex = UnicodeEncodeError("latin-1", "页面", 0, 2, "ordinal not in range(256)")
    assert classify_upload_error(ex) == "local_error"
    assert is_local_error("local_error")


def test_classify_value_and_type_error_are_local_error():
    assert classify_upload_error(ValueError("bad url")) == "local_error"
    assert classify_upload_error(TypeError("data must be bytes")) == "local_error"


def test_classify_network_errors_unchanged():
    """回归钉：网络类错误分型一个字不变。"""
    assert classify_upload_error(OSError("down")) == "upstream_unreachable"
    assert classify_upload_error(socket.timeout("t")) == "upstream_unreachable"
    assert classify_upload_error(urllib.error.URLError(OSError("x"))) == "upstream_unreachable"
    assert classify_upload_error(urllib.error.URLError(socket.gaierror(11001, "x"))) == "upstream_dns_failed"
    assert classify_upload_error(
        urllib.error.HTTPError("u", 413, "big", None, None)) == "upload_rejected_413"
    assert not is_local_error("upstream_unreachable")


# ── ② 真 http.client 往返 ──────────────────────────────────────────────────────

class _Recorder(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        _Recorder.seen.append({"meta": self.headers.get("x-diag-meta"), "bytes": len(body),
                               "path": self.path})
        out = json.dumps({"ok": True, "code": "424242"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):  # 静音
        return


@pytest.fixture
def local_site():
    _Recorder.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Recorder)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.asyncio
async def test_build_and_upload_cjk_note_reaches_server(tmp_path, local_site, monkeypatch):
    """带中文 note 真发到本地服务：头可编码、服务端能还原中文、一次成功零重试。"""
    cm = _CM(tmp_path)
    monkeypatch.setattr(du, "site_url", lambda _cm: local_site)
    with patch("src.utils.diagnostic_bundle.build_diagnostic_bundle",
               return_value=b"PK-full-bundle"):
        out = await build_and_upload(cm, note=CJK_NOTE)
    assert out == {"ok": True, "code": "424242"}
    assert len(_Recorder.seen) == 1
    meta = json.loads(_Recorder.seen[0]["meta"])
    assert meta["note"] == CJK_NOTE
    assert _Recorder.seen[0]["bytes"] == len(b"PK-full-bundle")
    # 没有任何东西落 outbox
    logs_dir = Path(cm.config_path).parent.parent / "logs"
    assert list_staged(logs_dir) == []


@pytest.mark.asyncio
async def test_flush_outbox_cjk_note_reaches_server(tmp_path, local_site, monkeypatch):
    """outbox 补传同一条头拼装口：暂存件带中文 note 也能补传成功。"""
    cm = _CM(tmp_path)
    logs = tmp_path / "logs"
    assert stage_bundle(logs, b"PK-staged", note=CJK_NOTE, fp="FP", ver="1.0.85")
    monkeypatch.setattr(du, "site_url", lambda _cm: local_site)
    out = flush_diag_outbox(cm)
    assert out["sent"] == 1 and out["remaining"] == 0
    meta = json.loads(_Recorder.seen[0]["meta"])
    assert meta["note"] == CJK_NOTE and meta["outbox_delayed"] is True


# ── ③ local_error 的处置语义 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_and_upload_local_error_no_retry_no_stage(tmp_path):
    """本机确定性错误：只发一次、不 mini、不暂存、error=local_error。"""
    cm = _CM(tmp_path)
    calls = {"n": 0}

    def _boom(req, timeout=0):
        calls["n"] += 1
        raise UnicodeEncodeError("latin-1", "页", 0, 1, "ordinal not in range(256)")

    t0 = time.monotonic()
    with patch("src.utils.diagnostic_bundle.build_diagnostic_bundle",
               return_value=b"PK-full-bundle"), \
         patch("urllib.request.urlopen", side_effect=_boom):
        out = await build_and_upload(cm, note="x")
    assert out == {"ok": False, "error": "local_error"}
    assert calls["n"] == 1                       # 没有重试、没有 mini
    assert time.monotonic() - t0 < du.RETRY_DELAY_SEC   # 没吃重试等待
    logs_dir = Path(cm.config_path).parent.parent / "logs"
    assert list_staged(logs_dir) == []           # 没暂存


def test_flush_local_error_drops_and_continues(tmp_path):
    """outbox 里一份本机永远发不出去的坏件：弃件并继续处理下一件，不 break 堵队列。"""
    cm = _CM(tmp_path)
    logs = tmp_path / "logs"
    stage_bundle(logs, b"PK-bad")
    time.sleep(0.02)
    stage_bundle(logs, b"PK-good")
    calls = {"n": 0}

    def _first_local_then_ok(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UnicodeEncodeError("latin-1", "页", 0, 1, "x")

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"ok": True, "code": "1"}).encode()
        return _R()

    with patch("urllib.request.urlopen", side_effect=_first_local_then_ok):
        out = flush_diag_outbox(cm)
    assert calls["n"] == 2
    assert out["dropped"] == 1 and out["sent"] == 1 and out["remaining"] == 0


def test_error_detail_local_error_bilingual():
    from src.web.i18n_packs import errors_stock
    assert "err.svc.diag_local_error" in errors_stock.ZH
    assert "err.svc.diag_local_error" in errors_stock.EN
    # 文案必须把人引向「复制全部信息」而不是「检查网络」
    assert "复制全部信息" in errors_stock.ZH["err.svc.diag_local_error"]
    assert "网络" not in errors_stock.ZH["err.svc.diag_local_error"].split("（")[0]


def test_source_has_single_header_builder():
    """两处 urlopen 上传口都走 diag_meta_header；源码里不得再出现 ensure_ascii=False 拼头。"""
    src = Path(du.__file__).read_text(encoding="utf-8")
    assert src.count('add_header("x-diag-meta", diag_meta_header(') == 2
    assert 'x-diag-meta", json.dumps' not in src
