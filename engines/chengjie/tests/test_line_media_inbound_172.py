# -*- coding: utf-8 -*-
"""#172（J-3 A，2026-09-05）：LINE 入站媒体「4 分钟就判已过期」→ 定层 + 三修。

真机定层（117 号 region PH + okline 随包的 LINE Chrome 3.7.2 源码 ``ltsmSandbox.js``）：

* 手机发的图 98.7h / 语音 98.7h / 自发语音 162.6h 仍全 200 ⇒ 4 分钟的 404 不是保留期。
* 同会话语音正常、图/视频 404 ⇒ 差在 **Letter Sealing 媒体**：Chrome ``yb()`` 把
  ``contentMetadata.e2eeVersion`` 非空的消息定位到 ``/r/talk/<SID>/<OID>``（e2ee-next
  ``SID=em``）并带 ``X-Talk-Meta``；对象是密文，钥匙 ``keyMaterial`` 在 chunks 解密后的
  JSON 里；HKDF(FileEncryption) → AES-CTR + HMAC-SHA256（视频按 128KB 分块哈希）。
  我们一律打 ``talk/m/<id>`` + 不解密 ⇒ 404。

本文件钉三件事：① 404 分档（NOT_FOUND 可重试 / EXPIRED 终局 / PENDING / ENCODING）；
② 对象定位 / X-Talk-Meta / 加解密与 Chrome 口径逐字节一致（加密侧在测试里按 ``gv()``
自证）；③ 入站链消费：miss 文案单源 + 回填排程。
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import threading
import time
import types

import pytest

import src.integrations.line_media as LM

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_stats():
    from src.integrations.line_media_stats import get_line_media_stats
    get_line_media_stats().reset()
    yield
    get_line_media_stats().reset()


def _ms_ago(sec: float) -> str:
    return str(int((time.time() - sec) * 1000))


# ─────────────────── ① 404 分档 ───────────────────

def test_classify_fresh_404_is_not_found():
    assert LM.classify_missing(3 * 60, 168) == LM.MISS_NOT_FOUND


def test_classify_30h_default_threshold_is_still_not_found():
    """真机：98h/162h 的对象仍 200 → 默认 7 天阈值下 30h 绝不许说「已过期」。"""
    assert LM.classify_missing(30 * 3600, LM.DEFAULT_EXPIRED_AFTER_HOURS) == LM.MISS_NOT_FOUND


def test_classify_beyond_threshold_is_expired():
    assert LM.classify_missing(8 * 86400, 168) == LM.MISS_EXPIRED
    # 运营把阈值调到 24h 时，30h 才是 EXPIRED（可配）
    assert LM.classify_missing(30 * 3600, 24) == LM.MISS_EXPIRED


def test_classify_unknown_age_never_expired():
    assert LM.classify_missing(-1, 168) == LM.MISS_NOT_FOUND
    assert LM.classify_missing(-1, 0.001) == LM.MISS_NOT_FOUND


def test_classify_server_states_win_over_age():
    assert LM.classify_missing(9 * 86400, 168, obj_status="uploading") == LM.MISS_PENDING
    assert LM.classify_missing(9 * 86400, 168, obj_status="incompleted") == LM.MISS_PENDING
    assert LM.classify_missing(60, 168, encode_status="ing") == LM.MISS_ENCODING


def test_default_threshold_is_seven_days_and_configurable():
    assert LM.DEFAULT_EXPIRED_AFTER_HOURS == 24 * 7
    cfg = LM.resolve_line_media_cfg(
        {"platform_login": {"line": {"media": {"expired_after_hours": 48}}}})
    assert cfg["expired_after_hours"] == 48.0
    bad = LM.resolve_line_media_cfg(
        {"platform_login": {"line": {"media": {"expired_after_hours": "x"}}}})
    assert bad["expired_after_hours"] == LM.DEFAULT_EXPIRED_AFTER_HOURS


def test_message_age_reads_created_time_ms():
    assert LM.line_message_age_sec({"createdTime": _ms_ago(120)}) == pytest.approx(120, abs=5)
    assert LM.line_message_age_sec({"createdTime": "0"}) == -1
    assert LM.line_message_age_sec({}) == -1
    assert LM.line_message_age_sec({"createdTime": "junk"}) == -1


class _Obs404:
    def __init__(self, info=None):
        self.calls = []
        self.info = info

    def download_object(self, service, sid, oid, **kw):
        import requests
        self.calls.append((service, sid, oid, kw.get("talk_meta")))
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError("404 Client Error: Not Found", response=resp)

    def object_info(self, path, **kw):
        self.info_call = (path, kw.get("talk_meta"))
        if self.info is None:
            raise RuntimeError("no info")
        return self.info


def _api(obs):
    return types.SimpleNamespace(obs=obs)


def test_download_404_fresh_video_reports_not_found_and_retryable(monkeypatch):
    """事故消息 630323359523275215：22:44 发、22:48 拉 → 必须是 NOT_FOUND 可重试。"""
    monkeypatch.setattr(LM.time, "sleep", lambda *_: None)
    obs = _Obs404(info={"status": "notexist"})
    msg = {"id": "630323359523275215", "contentType": LM.CT_VIDEO,
           "createdTime": _ms_ago(4 * 60), "contentMetadata": {"DURATION": "9800"}}
    out: dict = {}
    kind, url = LM.download_line_media(_api(obs), msg, "acct", out=out)
    assert (kind, url) == ("video", "")
    assert out["reason"] == LM.MISS_NOT_FOUND and out["retryable"] is True
    assert out["expired"] is False and out["http_status"] == 404
    assert out["obs_status"] == "notexist"
    assert out["age_sec"] == pytest.approx(240, abs=5)
    # 非加密：只有 talk/m/<id> 一条候选路径，不带 X-Talk-Meta
    assert obs.calls == [("talk", "m", "630323359523275215", None)]


def test_download_404_old_message_is_expired(monkeypatch):
    monkeypatch.setattr(LM.time, "sleep", lambda *_: None)
    obs = _Obs404(info={"status": "notexist"})
    msg = {"id": "M", "contentType": LM.CT_IMAGE, "createdTime": _ms_ago(9 * 86400)}
    out: dict = {}
    LM.download_line_media(_api(obs), msg, "acct", out=out)
    assert out["reason"] == LM.MISS_EXPIRED and out["expired"] is True
    assert out["retryable"] is False


def test_download_404_uploading_is_pending_even_if_old(monkeypatch):
    monkeypatch.setattr(LM.time, "sleep", lambda *_: None)
    obs = _Obs404(info={"status": "uploading"})
    msg = {"id": "M", "contentType": LM.CT_VIDEO, "createdTime": _ms_ago(9 * 86400)}
    out: dict = {}
    LM.download_line_media(_api(obs), msg, "acct", out=out)
    assert out["reason"] == LM.MISS_PENDING and out["retryable"] is True


def test_download_404_encoding_is_retryable(monkeypatch):
    monkeypatch.setattr(LM.time, "sleep", lambda *_: None)
    obs = _Obs404(info={"status": "exist", "encodeStatus": "ing"})
    msg = {"id": "M", "contentType": LM.CT_VIDEO, "createdTime": _ms_ago(30)}
    out: dict = {}
    LM.download_line_media(_api(obs), msg, "acct", out=out)
    assert out["reason"] == LM.MISS_ENCODING and out["retryable"] is True


def test_object_info_failure_falls_back_to_age_rule(monkeypatch):
    monkeypatch.setattr(LM.time, "sleep", lambda *_: None)
    obs = _Obs404(info=None)  # object_info 抛
    msg = {"id": "M", "contentType": LM.CT_IMAGE, "createdTime": _ms_ago(60)}
    out: dict = {}
    LM.download_line_media(_api(obs), msg, "acct", out=out)
    assert out["reason"] == LM.MISS_NOT_FOUND and out["obs_status"] == ""


def test_download_404_logs_kind_id_status_age(monkeypatch, caplog):
    """指令验收：重试/首拉的 404 写一行 kind/id/status/age（WARNING 级客户机可见）。"""
    import logging
    monkeypatch.setattr(LM.time, "sleep", lambda *_: None)
    msg = {"id": "ID9", "contentType": LM.CT_VIDEO, "createdTime": _ms_ago(3 * 60)}
    with caplog.at_level(logging.WARNING, logger="src.integrations.line_media"):
        LM.download_line_media(_api(_Obs404()), msg, "acctZ", out={})
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "kind=video" in line and "id=ID9" in line and "acctZ" in line
    assert "http=404" in line and "age=3 分钟" in line and "reason=not_found" in line
    assert "已过期" not in line


# ─────────────────── ② 对象定位 / X-Talk-Meta / 加解密 ───────────────────

def test_locator_plain_message_is_talk_m_id():
    assert LM.obs_object_locator({"id": "1", "contentType": 1}) == ("m", "1")
    assert LM.obs_object_locator({"id": "1", "contentType": 1}, preview=True) == ("m", "1/preview")


def test_locator_original_quality_uses_original_tid():
    msg = {"id": "7", "contentType": 1,
           "contentMetadata": {"MEDIA_CONTENT_INFO": json.dumps({"category": "original"})}}
    assert LM.obs_object_locator(msg) == ("m", "7/original")
    # 兜底候选：主路径 + 基路径，去重
    assert LM.obs_object_candidates(msg) == [("m", "7/original"), ("m", "7")]


def test_locator_e2ee_uses_sid_oid_pop():
    msg = {"id": "9", "contentType": 2,
           "contentMetadata": {"e2eeVersion": "2", "SID": "em", "OID": "obj-9", "OBS_POP": "jp1"}}
    assert LM.is_e2ee_media(msg) is True
    assert LM.obs_object_locator(msg) == ("em", "obj-9?p=jp1")
    assert LM.obs_object_locator(msg, preview=True) == ("em", "obj-9__ud-preview?p=jp1")
    assert LM.obs_object_candidates(msg) == [("em", "obj-9?p=jp1"), ("m", "9")]


def test_locator_e2ee_defaults_to_m_and_message_id():
    msg = {"id": "9", "contentType": 1, "contentMetadata": {"e2eeVersion": "2"}}
    assert LM.obs_object_locator(msg) == ("m", "9")
    assert LM.obs_object_candidates(msg) == [("m", "9")]


def test_talk_meta_matches_chrome_se_encoding():
    """SE(id)：thrift 二进制 Message{4:id(STRING), 27:[](LIST<STRUCT>)} STOP → b64 → JSON → b64。"""
    tm = LM.build_talk_meta("630323359523275215")
    outer = json.loads(base64.b64decode(tm))
    assert set(outer) == {"message"}
    raw = base64.b64decode(outer["message"])
    mid = b"630323359523275215"
    expect = (b"\x0b\x00\x04" + len(mid).to_bytes(4, "big") + mid
              + b"\x0f\x00\x1b\x0c\x00\x00\x00\x00" + b"\x00")
    assert raw == expect


def test_hkdf_matches_cryptography_oracle():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    ikm = os.urandom(32)
    ours = LM._hkdf_sha256(ikm, b"FileEncryption", 76)
    theirs = HKDF(algorithm=hashes.SHA256(), length=76, salt=None,
                  info=b"FileEncryption").derive(ikm)
    assert ours == theirs
    enc, mac, nonce = LM.derive_media_keys(base64.b64encode(ikm).decode())
    assert (enc, mac, nonce) == (theirs[:32], theirs[32:64], theirs[64:76])


@pytest.mark.parametrize("is_video,size", [(False, 5000), (True, 5000), (True, 131072 * 3 + 17)])
def test_media_encrypt_decrypt_roundtrip(is_video, size):
    km = base64.b64encode(os.urandom(32)).decode()
    plain = os.urandom(size)
    blob = LM.encrypt_e2ee_media(plain, km, is_video=is_video)
    assert len(blob) == size + 32
    assert LM.decrypt_e2ee_media(blob, km, is_video=is_video) == plain


def test_media_decrypt_rejects_tamper_and_wrong_key():
    km = base64.b64encode(os.urandom(32)).decode()
    blob = bytearray(LM.encrypt_e2ee_media(b"x" * 1000, km))
    blob[10] ^= 0x01
    with pytest.raises(LM.E2EEMediaError):
        LM.decrypt_e2ee_media(bytes(blob), km)
    good = LM.encrypt_e2ee_media(b"x" * 1000, km)
    with pytest.raises(LM.E2EEMediaError):
        LM.decrypt_e2ee_media(good, base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(LM.E2EEMediaError):
        LM.decrypt_e2ee_media(b"short", km)


def test_video_hmac_is_over_chunk_hashes_not_body():
    """Chrome ``vv()``：视频 HMAC 输入＝128KB 分块 SHA-256 拼接（与 image 口径不同）。"""
    body = os.urandom(131072 * 2 + 5)
    assert LM._media_hmac_input(body, is_video=False) == body
    import hashlib
    parts = [hashlib.sha256(body[i:i + 131072]).digest() for i in range(0, len(body), 131072)]
    assert LM._media_hmac_input(body, is_video=True) == b"".join(parts)


class _ObsE2EE:
    """只在 Chrome 口径的加密路径上有对象；talk/m/<id> 一律 404。"""

    def __init__(self, blob, *, sid="em", oid="obj-1"):
        self.blob = blob
        self.sid, self.oid = sid, oid
        self.calls = []

    def download_object(self, service, sid, oid, **kw):
        import requests
        self.calls.append((service, sid, oid, kw.get("talk_meta")))
        if (sid, oid.split("?")[0]) == (self.sid, self.oid):
            return self.blob
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError("404", response=resp)


def test_e2ee_media_downloads_via_em_path_and_decrypts(monkeypatch, tmp_path):
    monkeypatch.setattr(LM.time, "sleep", lambda *_: None)
    from src.integrations import protocol_bridge as PB
    monkeypatch.setattr(PB, "media_paths",
                        lambda platform, name, ext: (str(tmp_path / (name + ext)),
                                                     f"/static/protocol_media/{platform}/{name}{ext}"))
    km = base64.b64encode(os.urandom(32)).decode()
    jpeg = b"\xff\xd8\xff\xe0" + os.urandom(3000)
    obs = _ObsE2EE(LM.encrypt_e2ee_media(jpeg, km), sid="em", oid="obj-1")
    monkeypatch.setattr(LM, "e2ee_media_material",
                        lambda api, message: {"keyMaterial": km})
    msg = {"id": "M1", "contentType": LM.CT_IMAGE, "createdTime": _ms_ago(10),
           "contentMetadata": {"e2eeVersion": "2", "SID": "em", "OID": "obj-1"},
           "chunks": ["a", "b", "c", "d", "e"]}
    out: dict = {}
    kind, url = LM.download_line_media(_api(obs), msg, "acct", out=out)
    assert kind == "image" and url.endswith("acct_M1.jpg")
    assert out["e2ee"] is True and out["reason"] == ""
    assert out["path"] == "/r/talk/em/obj-1"
    # 只打了加密路径一次，且带 X-Talk-Meta
    assert obs.calls == [("talk", "em", "obj-1", LM.build_talk_meta("M1"))]
    with open(tmp_path / "acct_M1.jpg", "rb") as fh:
        assert fh.read() == jpeg, "落盘必须是解密后的明文（magic FFD8）"


def test_e2ee_video_decrypts_with_chunked_hmac(monkeypatch, tmp_path):
    monkeypatch.setattr(LM.time, "sleep", lambda *_: None)
    from src.integrations import protocol_bridge as PB
    monkeypatch.setattr(PB, "media_paths",
                        lambda platform, name, ext: (str(tmp_path / (name + ext)),
                                                     f"/static/x/{name}{ext}"))
    km = base64.b64encode(os.urandom(32)).decode()
    mp4 = b"\x00\x00\x00\x18ftypisom" + os.urandom(131072 * 2 + 100)
    obs = _ObsE2EE(LM.encrypt_e2ee_media(mp4, km, is_video=True), sid="em", oid="V")
    monkeypatch.setattr(LM, "e2ee_media_material", lambda api, message: {"keyMaterial": km})
    msg = {"id": "V", "contentType": LM.CT_VIDEO, "createdTime": _ms_ago(10),
           "contentMetadata": {"e2eeVersion": "2", "SID": "em"}, "chunks": ["a", "b", "c"]}
    out: dict = {}
    kind, url = LM.download_line_media(_api(obs), msg, "acct", out=out)
    assert kind == "video" and url
    with open(tmp_path / "acct_V.mp4", "rb") as fh:
        assert fh.read()[4:8] == b"ftyp"


def test_e2ee_without_key_material_skips_download(monkeypatch):
    """没钥匙别去下密文（白占磁盘还给不了坐席）——记 e2ee_no_key，文案指路手机。"""
    monkeypatch.setattr(LM.time, "sleep", lambda *_: None)
    obs = _ObsE2EE(b"cipher", sid="em", oid="obj-1")
    monkeypatch.setattr(LM, "e2ee_media_material", lambda api, message: {})
    msg = {"id": "M1", "contentType": LM.CT_IMAGE, "createdTime": _ms_ago(10),
           "contentMetadata": {"e2eeVersion": "2", "SID": "em", "OID": "obj-1"},
           "chunks": ["a", "b", "c"]}
    out: dict = {}
    kind, url = LM.download_line_media(_api(obs), msg, "acct", out=out)
    assert (kind, url) == ("image", "")
    assert out["reason"] == LM.MISS_E2EE_NO_KEY and obs.calls == []
    assert out["retryable"] is False
    txt = LM.inbound_miss_text("image", out)
    assert "Letter Sealing" in txt and "手机" in txt


def test_e2ee_hmac_mismatch_is_retryable_decrypt_failure(monkeypatch):
    monkeypatch.setattr(LM.time, "sleep", lambda *_: None)
    km = base64.b64encode(os.urandom(32)).decode()
    obs = _ObsE2EE(b"\x00" * 100, sid="em", oid="obj-1")  # 不是合法密文
    monkeypatch.setattr(LM, "e2ee_media_material", lambda api, message: {"keyMaterial": km})
    msg = {"id": "M1", "contentType": LM.CT_IMAGE, "createdTime": _ms_ago(10),
           "contentMetadata": {"e2eeVersion": "2", "SID": "em", "OID": "obj-1"},
           "chunks": ["a", "b", "c"]}
    out: dict = {}
    LM.download_line_media(_api(obs), msg, "acct", out=out)
    assert out["reason"] == LM.MISS_E2EE_DECRYPT and out["retryable"] is True
    assert "hmac_mismatch" in out["detail"]


def test_e2ee_material_extracts_key_material_from_plaintext(monkeypatch):
    """复用 okline 的信道/解密原语，但拿**全量**明文（okline 自己只留 text）。"""
    calls = {}

    class _Bridge:
        def e2ee_decrypt_v2(self, channel, **kw):
            calls["v2"] = kw
            plain = json.dumps({"keyMaterial": "S0VZ", "fileName": "a.pdf"}).encode()
            return base64.b64encode(plain).decode()

        def e2ee_decrypt_v1(self, channel, **kw):
            raise AssertionError("V2 消息不该走 V1")

    class _E2EE:
        _bridge = _Bridge()

        def is_ready(self):
            return True

        @staticmethod
        def _is_group(m):
            return False

        def _channel_for_receive(self, sender, skid, rkid):
            calls["chan"] = (sender, skid, rkid)
            return 42

    from okline import e2ee_crypto as fr
    chunks = fr.build_chunks(os.urandom(60), 11, 22)
    msg = {"id": "M", "from": "Usender", "to": "Ume", "contentType": 1,
           "contentMetadata": {"e2eeVersion": "2"}, "chunks": chunks}
    got = LM.e2ee_media_material(types.SimpleNamespace(e2ee=_E2EE()), msg)
    assert got == {"keyMaterial": "S0VZ", "fileName": "a.pdf"}
    assert calls["chan"] == ("Usender", 11, 22)
    assert calls["v2"]["content_type"] == 1


def test_e2ee_material_soft_fails():
    assert LM.e2ee_media_material(object(), {"chunks": ["a", "b", "c"]}) == {}
    assert LM.e2ee_media_material(types.SimpleNamespace(e2ee=None), {"chunks": ["a", "b", "c"]}) == {}
    assert LM.e2ee_media_material(types.SimpleNamespace(e2ee=object()), {"id": "x"}) == {}


# ─────────────────── ③ 文案 + 入站链消费 ───────────────────

def test_miss_text_by_reason():
    assert "已过期" in LM.inbound_miss_text("video", {"reason": LM.MISS_EXPIRED, "expired": True})
    nf = LM.inbound_miss_text("video", {"reason": LM.MISS_NOT_FOUND, "age_sec": 240})
    assert "[视频]" in nf and "404" in nf and "重试" in nf and "4 分钟" in nf and "已过期" not in nf
    assert "转码" in LM.inbound_miss_text("video", {"reason": LM.MISS_ENCODING})
    assert "上传" in LM.inbound_miss_text("image", {"reason": LM.MISS_PENDING})
    # 瞬态/开关类不改文案（维持「[图片]」占位）
    assert LM.inbound_miss_text("image", {"reason": "download_error"}) == ""
    assert LM.inbound_miss_text("image", {"reason": "disabled"}) == ""
    assert LM.inbound_miss_text("image", None) == ""


def test_retryable_set_excludes_terminal_reasons():
    assert LM.MISS_EXPIRED not in LM.RETRYABLE_MISS
    assert LM.MISS_E2EE_NO_KEY not in LM.RETRYABLE_MISS
    for r in (LM.MISS_NOT_FOUND, LM.MISS_PENDING, LM.MISS_ENCODING, LM.MISS_E2EE_DECRYPT):
        assert r in LM.RETRYABLE_MISS


def _worker_stub():
    from src.integrations.account_orchestrator import LineProtocolWorker
    w = LineProtocolWorker.__new__(LineProtocolWorker)
    w.account_id = "acct"
    w.config = {}
    w.client = object()
    w._loop = None
    return w


def test_schedule_media_retry_backfills_row(monkeypatch):
    """首拉失败 → Timer 到点重拉 → 成功即按 (会话, 平台消息 id) 回填媒体 + 换掉提示文案。"""
    from src.integrations import account_orchestrator as AO
    w = _worker_stub()
    timers = []

    class _Timer:
        def __init__(self, delay, fn):
            self.delay, self.fn = delay, fn
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", _Timer)
    results = iter([("video", ""), ("video", "/static/protocol_media/line/acct_M.mp4")])
    outs = iter([{"reason": LM.MISS_ENCODING, "retryable": True, "http_status": 404}, {}])

    def _inbound(message, *, is_group, out=None):
        r = next(results)
        if out is not None:
            out.update(next(outs))
        return r

    monkeypatch.setattr(w, "_inbound_media", _inbound)

    class _Store:
        def __init__(self):
            self.media, self.texts = [], []
            self.row = {"text": "[视频] 对方的视频仍在 LINE 服务端处理（转码）中，稍后点击重试"}

        def update_message_media(self, cid, **kw):
            self.media.append((cid, kw))
            return True

        def get_message(self, mid):
            return dict(self.row, message_id=mid)

        def update_message_text(self, cid, **kw):
            self.texts.append((cid, kw))
            return True

    store = _Store()
    from src.integrations import protocol_bridge as PB
    monkeypatch.setattr(PB, "get_inbox_store", lambda: store)
    hint = store.row["text"]
    w._schedule_media_retry({"id": "M", "contentType": 2}, chat_key="Upeer",
                            is_group=False, kind="video", hint=hint)
    assert len(timers) == 1 and timers[0].delay == AO.LineProtocolWorker._MEDIA_RETRY_DELAYS[0]
    timers[0].fn()          # 第一次重拉：仍 encoding → 排第二次
    assert len(timers) == 2 and timers[1].delay == AO.LineProtocolWorker._MEDIA_RETRY_DELAYS[1]
    assert store.media == []
    timers[1].fn()          # 第二次：拉到了
    assert store.media == [("line:acct:Upeer", {
        "media_type": "video", "media_ref": "/static/protocol_media/line/acct_M.mp4",
        "platform_msg_id": "M"})]
    assert store.texts and store.texts[0][1]["text"] == "[视频]"
    assert store.texts[0][1]["only_if_empty"] is False
    assert len(timers) == 2, "两次用尽即止，不再排程"


def test_schedule_media_retry_stops_when_not_retryable(monkeypatch):
    w = _worker_stub()
    timers = []

    class _Timer:
        def __init__(self, delay, fn):
            self.fn = fn
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", _Timer)

    def _inbound(message, *, is_group, out=None):
        if out is not None:
            out.update({"reason": LM.MISS_EXPIRED, "retryable": False, "expired": True})
        return "image", ""

    monkeypatch.setattr(w, "_inbound_media", _inbound)
    w._schedule_media_retry({"id": "M", "contentType": 1}, chat_key="U", is_group=False, kind="image")
    timers[0].fn()
    assert len(timers) == 1, "终局（已过期）不再排程"
    # 无 id / 无 chat_key 直接不排
    w._schedule_media_retry({"contentType": 1}, chat_key="U", is_group=False, kind="image")
    w._schedule_media_retry({"id": "M"}, chat_key="", is_group=False, kind="image")
    assert len(timers) == 1


def test_ingest_wiring_uses_single_source_text_and_retry():
    src = (REPO / "src/integrations/account_orchestrator.py").read_text(encoding="utf-8")
    i = src.index("def _ingest_inbound(")
    seg = src[i:i + 6000]
    assert "inbound_miss_text(media_type, _media_miss)" in seg
    assert '_media_miss.get("retryable")' in seg
    assert "_schedule_media_retry(" in seg
    assert "expired_media_text(media_type)" not in seg, "已收口到 inbound_miss_text 单源"


def test_download_line_media_never_raises_on_weird_e2ee_input(monkeypatch):
    class _Obs:
        def download_object(self, *a, **k):
            raise RuntimeError("boom")

    msg = {"id": "M", "contentType": LM.CT_IMAGE, "contentMetadata": {"e2eeVersion": "2"},
           "chunks": "not-a-list"}
    out: dict = {}
    assert LM.download_line_media(_api(_Obs()), msg, "a", out=out) == ("image", "")
    assert out["reason"] == LM.MISS_E2EE_NO_KEY
