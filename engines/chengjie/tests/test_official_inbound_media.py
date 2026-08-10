"""IG / Zalo 官方通道入站媒体可见化门禁（P1，2026-08-05）。

修的洞：Phase I1 把「客户发图 → 收件箱占位可见」铺到了 WA/LINE/Messenger 官方链，
IG/Zalo 是漏网——媒体消息被抽取层整条丢弃，坐席端连 [图片] 占位都没有，客户发了
照片以为已读，实际系统里查无此事。

不变量：
- 媒体抽取与文字抽取**分离**（extract_zalo_messages 的「仅 user_send_text」语义
  有既有门禁钉死，不动）；
- 镜像带 media_ref（CDN URL）——为下一阶段「AI 识图」预留取件路径；
- ``unsupported_type_reply`` 显式空串 = 沉默（陪伴人设下自动回「仅支持文字」会穿帮），
  键缺席才落默认话术。
"""

from __future__ import annotations

from src.integrations.instagram_webhook import extract_ig_media
from src.integrations.zalo_webhook import extract_zalo_media

# ── 抽取纯函数 ──────────────────────────────────────────────────────────────


def _ig_body(messaging):
    return {"object": "instagram", "entry": [{"messaging": messaging}]}


def test_ig_media_extracts_attachment_message():
    out = extract_ig_media(_ig_body([{
        "sender": {"id": "IGS1"},
        "message": {"mid": "m1", "attachments": [
            {"type": "image", "payload": {"url": "https://cdn/x.jpg"}},
            {"type": "image", "payload": {"url": "https://cdn/y.jpg"}},
        ]},
    }]))
    assert out == [{"sender": "IGS1", "mid": "m1", "media_type": "image",
                    "url": "https://cdn/x.jpg", "count": 2}]


def test_ig_media_skips_echo_text_and_foreign_object():
    assert extract_ig_media({"object": "page", "entry": []}) == []
    out = extract_ig_media(_ig_body([
        {"sender": {"id": "A"}, "message": {
            "is_echo": True,
            "attachments": [{"type": "image", "payload": {}}]}},
        {"sender": {"id": "B"}, "message": {
            "text": "hi",
            "attachments": [{"type": "image", "payload": {}}]}},  # 有文字走文字链
        {"sender": {"id": "C"}, "message": {}},                    # 无附件
    ]))
    assert out == []


def test_ig_media_type_falls_back_to_file():
    out = extract_ig_media(_ig_body([{
        "sender": {"id": "D"},
        "message": {"mid": "m2", "attachments": [{"payload": {}}]},
    }]))
    assert out[0]["media_type"] == "file" and out[0]["url"] == ""


def test_zalo_media_events_mapped():
    body = {"event_name": "user_send_image", "sender": {"id": "U1"},
            "message": {"msg_id": "z1", "attachments": [
                {"type": "image", "payload": {"url": "https://z/x.jpg"}}]}}
    assert extract_zalo_media(body) == [{
        "sender": "U1", "msg_id": "z1", "media_type": "image",
        "url": "https://z/x.jpg"}]
    # 事件名 → 类型映射逐个可用（location 无 url 也不崩）
    for ev, mt in (("user_send_sticker", "sticker"), ("user_send_gif", "gif"),
                   ("user_send_audio", "audio"), ("user_send_video", "video"),
                   ("user_send_file", "file"), ("user_send_location", "location")):
        got = extract_zalo_media({"event_name": ev, "sender": {"id": "U2"},
                                  "message": {}})
        assert got and got[0]["media_type"] == mt, ev


def test_zalo_media_ignores_text_event_and_missing_sender():
    assert extract_zalo_media({"event_name": "user_send_text",
                               "sender": {"id": "U"}, "message": {"text": "x"}}) == []
    assert extract_zalo_media({"event_name": "user_send_image"}) == []


def test_zalo_media_url_falls_back_to_thumbnail():
    body = {"event_name": "user_send_video", "sender": {"id": "U3"},
            "message": {"attachments": [{"payload": {"thumbnail": "https://z/t.jpg"}}]}}
    assert extract_zalo_media(body)[0]["url"] == "https://z/t.jpg"


# ── 处理器：镜像 + 可选回复 ─────────────────────────────────────────────────


async def test_ig_media_handler_mirrors_and_replies(monkeypatch):
    from src.integrations import instagram_webhook as ig
    mirrored, sent = [], []
    monkeypatch.setattr(
        "src.integrations.shared.official_inbound.mirror_inbound_media",
        lambda **kw: mirrored.append(kw) or True)

    async def _send(sender, text, ig_id, token, account_id=""):
        sent.append((sender, text))
        return {"ok": True}
    monkeypatch.setattr(ig, "ig_send_text", _send)

    item = {"sender": "IGS1", "mid": "m1", "media_type": "image",
            "url": "https://cdn/x.jpg", "count": 1}
    await ig._handle_ig_media(item=item, ig_id="IGID", ig_account_id="IGID",
                              page_token="T", unsupported="仅支持文字")
    assert mirrored and mirrored[0]["platform"] == "instagram"
    assert mirrored[0]["chat_key"] == "ig:user:IGS1"
    assert mirrored[0]["media_type"] == "image"
    assert mirrored[0]["media_ref"] == "https://cdn/x.jpg", "URL 必须随占位入库（识图预留）"
    assert sent == [("IGS1", "仅支持文字")]


async def test_ig_media_handler_silent_when_reply_blank(monkeypatch):
    from src.integrations import instagram_webhook as ig
    mirrored = []
    monkeypatch.setattr(
        "src.integrations.shared.official_inbound.mirror_inbound_media",
        lambda **kw: mirrored.append(kw) or True)

    async def _boom(*a, **k):
        raise AssertionError("unsupported='' 时不该回话")
    monkeypatch.setattr(ig, "ig_send_text", _boom)
    monkeypatch.setattr(ig, "ig_send_with_window_fallback", _boom)

    await ig._handle_ig_media(
        item={"sender": "IGS2", "mid": "m", "media_type": "video", "url": ""},
        ig_id="IGID", ig_account_id="IGID", page_token="T", unsupported="")
    assert mirrored, "沉默 ≠ 不可见：占位镜像必须照做"


async def test_zalo_media_handler_mirrors_and_replies(monkeypatch):
    from src.integrations import zalo_webhook as z
    mirrored, sent = [], []
    monkeypatch.setattr(
        "src.integrations.shared.official_inbound.mirror_inbound_media",
        lambda **kw: mirrored.append(kw) or True)

    async def _send(user_id, text, token, *, message_type="cs", account_id=""):
        sent.append((user_id, text))
        return {"ok": True}
    monkeypatch.setattr(z, "zalo_send_text", _send)

    await z._handle_zalo_media(
        item={"sender": "U1", "msg_id": "z1", "media_type": "image",
              "url": "https://z/x.jpg"},
        access_token="tok", oa_account_id="official", unsupported="仅支持文字")
    assert mirrored[0]["chat_key"] == "zalo:user:U1"
    assert mirrored[0]["media_ref"] == "https://z/x.jpg"
    assert sent == [("U1", "仅支持文字")]


# ── unsupported 语义：显式空串=沉默，缺席=默认 ─────────────────────────────

def test_unsupported_reply_blank_optout_semantics():
    """两个 webhook 的 register 段必须保留「显式空串=沉默」判定（_raw_unsup is None）。

    旧写法 ``(cfg.get(...) or "").strip() or 默认`` 让空串永远配不出来——陪伴人设
    部署想关掉「仅支持文字」自动回复没有任何出路。源码级钉住防回退。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "integrations"
    for name in ("instagram_webhook.py", "zalo_webhook.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "_raw_unsup is None" in src, f"{name} 丢了显式空串=沉默的语义"
