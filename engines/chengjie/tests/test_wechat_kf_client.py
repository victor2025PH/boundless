# -*- coding: utf-8 -*-
"""微信客服 API 客户端 / 加解密 / 消息归一 门禁（实施97 线 A）。

全部离线：HTTP 经注入的 ``request_fn`` 假服务器；加解密除回环外还钉企业微信官方示例向量
（token/EncodingAESKey/CorpID 与密文取自官方 WXBizMsgCrypt 示例）——回环只能证明「自己
加的自己能解」，官方向量才证明「企微加的我们能解」。
"""
from __future__ import annotations

import json

import pytest

from src.integrations import wechat_kf as WK

# ── 键契约 ──────────────────────────────────────────────────────────────────

def test_chat_key_roundtrip_and_idempotent():
    assert WK.chat_key_for("wmAJ2GCAAAme1XQ") == "wxkf:user:wmAJ2GCAAAme1XQ"
    assert WK.chat_key_for("wxkf:user:abc") == "wxkf:user:abc"
    assert WK.chat_key_for("") == ""
    assert WK.external_userid_from_chat_key("wxkf:user:abc") == "abc"
    assert WK.external_userid_from_chat_key("abc") == "abc"
    assert WK.external_userid_from_chat_key("wechat_kf:user:abc") == "abc"


def test_truncate_text_keeps_short_and_caps_long():
    assert WK.truncate_text("  你好 ") == "你好"
    long = "字" * 700
    out = WK.truncate_text(long)
    assert len(out) == WK.TEXT_MAX_CHARS and out.endswith("…")


# ── 消息归一 ────────────────────────────────────────────────────────────────

def _base(**kw):
    d = {"msgid": "from_msgid_1", "open_kfid": "wkKF", "external_userid": "wmU1",
         "send_time": 1700000000, "origin": 3}
    d.update(kw)
    return d


def test_normalize_text_with_menu_id():
    n = WK.normalize_kf_message(_base(msgtype="text", text={"content": "你好", "menu_id": "m1"}))
    assert n["kind"] == "message" and n["text"] == "你好" and n["menu_id"] == "m1"
    assert n["ts"] == 1700000000.0 and n["origin"] == 3
    assert n["external_userid"] == "wmU1" and n["open_kfid"] == "wkKF"


@pytest.mark.parametrize("mt,mtype", [("image", "image"), ("voice", "voice"),
                                      ("video", "video"), ("file", "file")])
def test_normalize_media_carries_media_id_and_empty_text(mt, mtype):
    n = WK.normalize_kf_message(_base(msgtype=mt, **{mt: {"media_id": "MID"}}))
    assert n["media_id"] == "MID" and n["media_type"] == mtype
    assert n["text"] == "", "媒体正文必须为空（占位由 media_type 承载，不污染 auto-draft）"


def test_normalize_location_link_card_miniprogram_placeholders():
    loc = WK.normalize_kf_message(_base(msgtype="location", location={
        "latitude": 23.1, "longitude": 113.3, "name": "广州塔", "address": "海珠区"}))
    assert loc["text"].startswith("[位置]") and "广州塔" in loc["text"] and "(23.1,113.3)" in loc["text"]
    link = WK.normalize_kf_message(_base(msgtype="link", link={"title": "官网", "url": "https://x.y"}))
    assert link["text"] == "[链接] 官网 https://x.y"
    card = WK.normalize_kf_message(_base(msgtype="business_card", business_card={"userid": "zhang"}))
    assert card["text"] == "[名片] zhang"
    mp = WK.normalize_kf_message(_base(msgtype="miniprogram", miniprogram={"title": "商城", "appid": "wxapp"}))
    assert mp["text"] == "[小程序] 商城 wxapp"


def test_normalize_event_extracts_type_and_ids_from_event_body():
    n = WK.normalize_kf_message({"msgtype": "event", "event": {
        "event_type": "msg_send_fail", "open_kfid": "wkKF", "external_userid": "wmU1",
        "fail_msgid": "x", "fail_type": 6}})
    assert n["kind"] == "event" and n["event_type"] == "msg_send_fail"
    assert n["event"]["fail_type"] == 6
    assert n["external_userid"] == "wmU1" and n["open_kfid"] == "wkKF"


def test_normalize_servicer_origin_and_unknown_type():
    n = WK.normalize_kf_message(_base(msgtype="text", origin=5, servicer_userid="lisi",
                                      text={"content": "人工回复"}))
    assert n["origin"] == WK.ORIGIN_SERVICER and n["servicer_userid"] == "lisi"
    u = WK.normalize_kf_message(_base(msgtype="weird_new_type"))
    assert u["kind"] == "message" and u["text"] == "[weird_new_type]"
    assert WK.normalize_kf_message({})["kind"] == "ignore"


def test_parse_sync_response_shape():
    items, cur, more = WK.parse_sync_response({
        "msg_list": [_base(msgtype="text", text={"content": "a"}), "junk"],
        "next_cursor": "C2", "has_more": 1})
    assert len(items) == 1 and cur == "C2" and more is True
    assert WK.parse_sync_response(None) == ([], "", False)


def test_errcode_classification():
    assert WK.classify_errcode(0) == ""
    assert WK.classify_errcode(42001) == "token" and WK.classify_errcode(40014) == "token"
    assert WK.classify_errcode(60020) == "ip_not_allowed"
    assert WK.classify_errcode(95014) == "servicer_not_active"
    assert WK.classify_errcode(12345) == "api_error" and WK.classify_errcode("x") == "api_error"


# ── 加解密 ──────────────────────────────────────────────────────────────────

_TOKEN = "QDG6eK"
_AESKEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
_CORPID = "wx5823bf96d3bd56c7"


def test_crypto_rejects_bad_key():
    with pytest.raises(ValueError):
        WK.KfCallbackCrypto("t", "short")


def test_crypto_roundtrip_and_signature():
    c = WK.KfCallbackCrypto(_TOKEN, _AESKEY, _CORPID)
    plain = "<xml><Event><![CDATA[kf_msg_or_event]]></Event><Token><![CDATA[T1]]></Token>" \
            "<OpenKfId><![CDATA[wkKF]]></OpenKfId></xml>".encode("utf-8")
    enc = c.encrypt(plain)
    msg, rid = c.decrypt(enc)
    assert msg == plain and rid == _CORPID
    sig = c.signature("1", "2", enc)
    body = f"<xml><ToUserName><![CDATA[{_CORPID}]]></ToUserName><Encrypt><![CDATA[{enc}]]></Encrypt></xml>".encode()
    fields = c.decrypt_callback(body, sig, "1", "2")
    assert fields == {"Event": "kf_msg_or_event", "Token": "T1", "OpenKfId": "wkKF"}
    # 签名不对 / receive_id 不对 → None（回 4xx），绝不抛
    assert c.decrypt_callback(body, "deadbeef", "1", "2") is None
    other = WK.KfCallbackCrypto(_TOKEN, _AESKEY, "other_corp")
    assert other.decrypt_callback(body, sig, "1", "2") is None
    assert c.decrypt_callback(b"<not xml", sig, "1", "2") is None


def test_crypto_official_sample_vector_url_verify():
    """企业微信官方 WXBizMsgCrypt 示例向量（URL 校验 echostr）：证明「企微加的我们能解」。

    密钥/签名/时间戳/nonce/echostr 均取自官方示例代码；解出的明文是官方给定的
    ``1616140317555161061``。签名或密钥布局哪一处漂了都会在这里红。
    """
    c = WK.KfCallbackCrypto(_TOKEN, _AESKEY, _CORPID)
    echo = c.verify_url(
        "5c45ff5e21c57e6ad56bac8758b79b1d9ac89fd3", "1409659589", "263014780",
        "P9nAzCzyDtyTWESHep1vC5X9xho/qYX3Zpb4yKa9SKld1DsH3Iyt3tP3zNdtp+4RPcs8TgAE7OaBO+FZXvnaqQ==")
    assert echo == "1616140317555161061"
    # 签名错 → None
    assert c.verify_url("bad", "1409659589", "263014780",
                        "P9nAzCzyDtyTWESHep1vC5X9xho/qYX3Zpb4yKa9SKld1DsH3Iyt3tP3zNdtp+4RPcs8TgAE7OaBO+FZXvnaqQ==") is None


# ── HTTP 客户端（假服务器） ──────────────────────────────────────────────────

class _FakeServer:
    """记录请求、按脚本回应。``script`` = [(status, dict)]；耗尽后回最后一条。"""

    def __init__(self):
        self.calls = []
        self.token_calls = 0
        self.token_ok = True
        self.responses = {}

    def set(self, path_suffix, *responses):
        self.responses[path_suffix] = list(responses)

    async def __call__(self, method, url, *, params=None, json=None, data=None, headers=None,
                       timeout=None):
        self.calls.append({"method": method, "url": url, "params": dict(params or {}),
                           "json": json, "data": data})
        if url.endswith("/gettoken"):
            self.token_calls += 1
            if not self.token_ok:
                return 200, _j({"errcode": 60020, "errmsg": "not allow to access from your ip"}), "application/json", {}
            return 200, _j({"errcode": 0, "access_token": f"TOK{self.token_calls}",
                            "expires_in": 7200}), "application/json", {}
        for suffix, resp in self.responses.items():
            if url.endswith(suffix):
                status, body = resp.pop(0) if len(resp) > 1 else resp[0]
                if isinstance(body, (bytes, bytearray)):
                    return status, bytes(body), "image/jpeg", {"Content-Disposition": 'attachment; filename="a.jpg"'}
                return status, _j(body), "application/json", {}
        return 200, _j({"errcode": 0}), "application/json", {}


def _j(d):
    return json.dumps(d).encode("utf-8")


def _client(server, now=None):
    clock = {"t": 1_000_000.0}

    def _now():
        return clock["t"]

    c = WK.WeChatKfClient("corp", "sec", request_fn=server, now=now or _now)
    return c, clock


async def test_token_cached_until_margin_then_refreshed():
    srv = _FakeServer()
    c, clock = _client(srv)
    t1 = await c.get_token()
    assert t1["ok"] and t1["data"]["access_token"] == "TOK1" and srv.token_calls == 1
    t2 = await c.get_token()
    assert t2["data"]["cached"] is True and srv.token_calls == 1
    clock["t"] += 7200 - 100  # 进入过期前 5 分钟余量 → 刷新
    t3 = await c.get_token()
    assert t3["data"]["access_token"] == "TOK2" and srv.token_calls == 2


async def test_token_failure_classifies_ip_whitelist():
    srv = _FakeServer()
    srv.token_ok = False
    c, _ = _client(srv)
    r = await c.get_token()
    assert r["ok"] is False and r["error_kind"] == "ip_not_allowed"
    assert "可信 IP" in r["errmsg"]
    # 缺凭证：不打网络
    empty = WK.WeChatKfClient("", "", request_fn=srv)
    r2 = await empty.get_token()
    assert r2["error_kind"] == "creds_missing"


async def test_api_refreshes_token_once_on_42001():
    srv = _FakeServer()
    srv.set("/kf/sync_msg", (200, {"errcode": 42001, "errmsg": "expired"}),
            (200, {"errcode": 0, "msg_list": [], "next_cursor": "C1", "has_more": 0}))
    c, _ = _client(srv)
    r = await c.sync_msg("wkKF", cursor="C0", token="T", limit=50)
    assert r["ok"] and r["data"]["next_cursor"] == "C1"
    assert srv.token_calls == 2, "42001 必须触发一次刷新重试"
    sync_calls = [x for x in srv.calls if x["url"].endswith("/kf/sync_msg")]
    assert len(sync_calls) == 2
    assert sync_calls[0]["json"] == {"open_kfid": "wkKF", "limit": 50, "voice_format": 0,
                                    "cursor": "C0", "token": "T"}
    assert sync_calls[1]["params"]["access_token"] == "TOK2"


async def test_send_text_normalizes_recipient_and_truncates():
    srv = _FakeServer()
    srv.set("/kf/send_msg", (200, {"errcode": 0, "msgid": "M1"}))
    c, _ = _client(srv)
    r = await c.send_text("wxkf:user:wmU1", "wkKF", "字" * 700, msgid="cli-1")
    assert r["ok"] and r["data"]["msgid"] == "M1"
    body = [x for x in srv.calls if x["url"].endswith("/kf/send_msg")][0]["json"]
    assert body["touser"] == "wmU1" and body["open_kfid"] == "wkKF"
    assert body["msgtype"] == "text" and len(body["text"]["content"]) == WK.TEXT_MAX_CHARS
    assert body["msgid"] == "cli-1"


async def test_send_media_maps_aliases_and_rejects_unknown():
    srv = _FakeServer()
    srv.set("/kf/send_msg", (200, {"errcode": 0, "msgid": "M2"}))
    c, _ = _client(srv)
    r = await c.send_media("wmU1", "wkKF", "photo", "MID")
    assert r["ok"]
    body = [x for x in srv.calls if x["url"].endswith("/kf/send_msg")][0]["json"]
    assert body["msgtype"] == "image" and body["image"] == {"media_id": "MID"}
    bad = await c.send_media("wmU1", "wkKF", "sticker", "MID")
    assert bad["ok"] is False and bad["error_kind"] == "not_supported"


async def test_api_error_carries_kind_and_last_error():
    srv = _FakeServer()
    srv.set("/kf/service_state/trans", (200, {"errcode": 95014, "errmsg": "servicer not active"}))
    c, _ = _client(srv)
    r = await c.trans_service_state("wkKF", "wmU1", WK.STATE_HUMAN, servicer_userid="lisi")
    assert r["ok"] is False and r["error_kind"] == "servicer_not_active" and r["errcode"] == 95014
    assert "95014" in c.last_error
    body = srv.calls[-1]["json"]
    assert body == {"open_kfid": "wkKF", "external_userid": "wmU1", "service_state": 3,
                    "servicer_userid": "lisi"}


async def test_download_media_distinguishes_json_error_from_bytes():
    srv = _FakeServer()
    srv.set("/media/get", (200, b"\xff\xd8\xff\xe0JPEG"))
    c, _ = _client(srv)
    r = await c.download_media("MID")
    assert r["ok"] and r["data"]["bytes"].startswith(b"\xff\xd8") and r["data"]["filename"] == "a.jpg"
    srv2 = _FakeServer()
    srv2.set("/media/get", (200, {"errcode": 40007, "errmsg": "invalid media_id"}))
    c2, _ = _client(srv2)
    r2 = await c2.download_media("MID")
    assert r2["ok"] is False and r2["errcode"] == 40007


async def test_network_exception_never_raises():
    async def boom(*a, **k):
        raise RuntimeError("dns down")

    c = WK.WeChatKfClient("corp", "sec", request_fn=boom)
    r = await c.get_token()
    assert r["ok"] is False and r["error_kind"] == "network"


def test_media_ext_resolution():
    assert WK.media_ext_for("image/jpeg", "image") == ".jpg"
    assert WK.media_ext_for("", "voice") == ".amr"
    assert WK.media_ext_for("application/octet-stream", "file", "report.PDF") == ".pdf"
    assert WK.media_ext_for("", "file") == ".bin"
