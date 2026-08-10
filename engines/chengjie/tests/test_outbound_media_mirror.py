"""A 线出站富媒体的收件箱镜像门禁（2026-07-31）。

**缘起（生产实测）**：Telegram 868 条出站消息里，按 ``media_type`` 只数得出 **1 条**
带媒体；而文本形态的媒体占位（``[图片] 配文`` / ``[语音]×3``）有 **166 条**。
同期 WhatsApp 212 条出站里 149 条是正规媒体行。根因不是 TG 发不出媒体（它发得好好的），
而是**两条链记账口径不同**：B 线走 ``orch.send_media`` 天生写媒体行，A 线（pyrogram
直发）只写一行纯文字。后果两条，都是实的：

1. 坐席在工作台**看不到自家人设发出去的图**，只有一行「[图片] 翻到一张之前拍的」；
2. 任何按 ``media_type`` 统计出站媒体的看板/报表把 A 线整条漏掉（1 vs 166）。

语音更甚：镜像里只有一个「[语音]」——坐席不知道 AI 说了什么，防复读读历史时看到的
也只是占位。故语音镜像改为带念稿（与 B 线 ``send_media(inbox_text=念稿)`` 同口径）。

**全部改动都是加法**：取不到 URL / 念稿为空 → 逐字退回改动前的纯文本镜像。
"""

import os
from pathlib import Path

import pytest

from src.client.sender import TelegramSenderMixin
from src.integrations import protocol_bridge as PB


# ─────────────────── publish_outbound_media ───────────────────

@pytest.fixture
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "static_media"
    monkeypatch.setattr(PB, "protocol_media_root", lambda: root)
    return root


def test_publish_copies_outside_file_and_returns_url(media_root, tmp_path):
    src = tmp_path / "selfie.jpg"
    src.write_bytes(b"\xff\xd8JPEG")
    url, mt = PB.publish_outbound_media("telegram", "acct1", str(src))
    assert mt == "image"
    assert url.startswith("/static/protocol_media/telegram/")
    local = PB.static_media_ref_to_path(url)
    assert local and Path(local).read_bytes() == b"\xff\xd8JPEG"


def test_publish_does_not_recopy_files_already_under_root(media_root):
    """已经在 protocol_media 下的文件直接推 URL——重复拷贝白吃磁盘。"""
    d = media_root / "telegram"
    d.mkdir(parents=True)
    f = d / "already_there.ogg"
    f.write_bytes(b"OGG")
    before = sorted(p.name for p in d.iterdir())
    url, mt = PB.publish_outbound_media("telegram", "acct1", str(f))
    assert url == "/static/protocol_media/telegram/already_there.ogg"
    assert mt == "voice"
    assert sorted(p.name for p in d.iterdir()) == before, "不该产生副本"


def test_publish_soft_fails_to_empty(media_root, tmp_path):
    """任何取不到的情况都回空串 → 调用方退回纯文本镜像（＝改动前行为）。"""
    assert PB.publish_outbound_media("telegram", "a", "") == ("", "")
    assert PB.publish_outbound_media(
        "telegram", "a", str(tmp_path / "nope.jpg")) == ("", "")
    assert PB.publish_outbound_media("telegram", "a", str(tmp_path)) == ("", "")


# ─────────────────── 语音镜像文案 ───────────────────

def test_voice_preview_carries_the_spoken_text():
    p = TelegramSenderMixin._voice_mirror_preview
    assert p("你好呀，今天怎么样") == "[语音] 你好呀，今天怎么样"
    assert p("第一句 第二句", 3) == "[语音]×3 第一句 第二句"
    # 多行/多空白折成单空格（镜像是一行预览）
    assert p("上句\n\n下句  尾") == "[语音] 上句 下句 尾"


def test_voice_preview_falls_back_to_bare_marker():
    """念稿为空 → 逐字回到改动前的「[语音]」，不产生「[语音] 」这种带尾空格的脏值。"""
    p = TelegramSenderMixin._voice_mirror_preview
    assert p("") == "[语音]"
    assert p("   ") == "[语音]"
    assert p(None) == "[语音]"
    assert p("", 2) == "[语音]×2"


# ─────────────────── 镜像透传 ───────────────────

class _Recorder(TelegramSenderMixin):
    """只装最小依赖的壳：捕获 _emit_inbox 的入参。"""

    def __init__(self):
        self.account_id = "acct1"
        self._mirror_inbox = False   # 不走 report_message_status 分支
        self.emitted = []

    def _emit_inbox(self, **kw):
        self.emitted.append(kw)


def test_mirror_forwards_media_fields():
    r = _Recorder()
    r._postsend_mirror_and_record(
        123, "[图片] 看这个", msg_id="9",
        media_type="image", media_ref="/static/protocol_media/telegram/x.jpg")
    assert r.emitted == [{
        "chat_id": 123, "text": "[图片] 看这个", "direction": "out",
        "msg_id": "9", "media_type": "image",
        "media_ref": "/static/protocol_media/telegram/x.jpg",
    }]


def test_mirror_without_media_is_unchanged_shape():
    """文本回复路径不带媒体参数时，emit 的字段值与改动前等价（空串）。"""
    r = _Recorder()
    r._postsend_mirror_and_record(7, "普通文本", msg_id="1")
    e = r.emitted[0]
    assert e["media_type"] == "" and e["media_ref"] == ""
    assert e["text"] == "普通文本" and e["direction"] == "out"


def test_mirror_text_override_splits_inbox_and_contacts(monkeypatch):
    """mirror_text（2026-08-02）：收件箱镜像行用干净念稿/配文（[语音]/[图片] 语义
    已由 media_type 承载），contacts 纯文本时间线仍保留标记表意——两个口径分开。"""
    import src.utils.companion_context as cc

    seen = {}
    monkeypatch.setattr(
        cc, "record_relationship_message",
        lambda acc, chat, direction, **k: seen.update(
            {"dir": direction, "prev": k.get("text_preview")}))
    r = _Recorder()
    r._postsend_mirror_and_record(
        5, "[语音] 念稿正文", msg_id="7", media_type="voice",
        media_ref="/static/protocol_media/telegram/a.ogg",
        mirror_text="念稿正文")
    assert r.emitted[0]["text"] == "念稿正文"          # 气泡不再重复方括号标记
    assert r.emitted[0]["media_type"] == "voice"
    assert seen == {"dir": "out", "prev": "[语音] 念稿正文"}  # contacts 保留标记


def test_mirror_row_sender_name_only_when_present():
    """sender_name（P1-3「谁的音色」）非空才透传——emit 形状对旧调用方零变化。"""
    r = _Recorder()
    r._mirror_out_row(1, "念稿", media_type="voice",
                      media_ref="/x.ogg", sender_name="林小雨")
    assert r.emitted[0]["sender_name"] == "林小雨"
    r2 = _Recorder()
    r2._mirror_out_row(1, "念稿", media_type="voice", media_ref="/x.ogg")
    assert "sender_name" not in r2.emitted[0]


def test_mirror_row_helper_does_not_touch_contacts(monkeypatch):
    """_mirror_out_row 是纯镜像口（分条语音逐条调用）：绝不能顺手记 contacts，
    否则一轮 3 条分条＝三倍亲密度。"""
    import src.utils.companion_context as cc

    hits = []
    monkeypatch.setattr(
        cc, "record_relationship_message",
        lambda *a, **k: hits.append(1))
    r = _Recorder()
    r._mirror_out_row(9, "第二条。", msg_id="2",
                      media_type="voice", media_ref="/static/x.ogg")
    assert len(r.emitted) == 1 and r.emitted[0]["text"] == "第二条。"
    assert hits == []


def test_publish_media_ref_never_raises(monkeypatch):
    """发布失败绝不能把发送流程带走——它只是镜像的锦上添花。"""
    r = _Recorder()
    assert r._publish_media_ref("/definitely/not/here.jpg") == ("", "")

    def _boom(*a, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(PB, "publish_outbound_media", _boom)
    assert r._publish_media_ref("/whatever.jpg") == ("", "")


# ─────────────────── 接线（防「函数加了没人调」）───────────────────

def _sender_src() -> str:
    return (Path(__file__).resolve().parents[1] / "src" / "client" / "sender.py"
            ).read_text(encoding="utf-8")


def test_photo_send_is_wired_to_publish():
    """``send_photo`` 必须发布 canonical 原图并把 media_ref 带进镜像。

    盯着 ``photo_path`` 而不是 ``_send_path``：后者是反封号去重微扰产出的**临时副本**，
    发完即删，拿它发布会得到一个指向已删除文件的 URL。
    """
    src = _sender_src()
    i = src.index("async def send_photo")
    body = src[i:i + 3000]
    assert "_publish_media_ref(photo_path)" in body, "应发布 canonical 原图"
    assert "_send_path)" not in body.split("_publish_media_ref")[1][:40]
    assert "media_ref=" in body


def test_voice_publishes_between_send_and_unlink():
    """音频发完即删 → 发布必须夹在「真发」与「删文件」之间。

    不能简单地拿文件里第一个 ``os.unlink(result.audio_path)`` 来比：它前面还有三处
    **拒发路径**的删除（拒 edge 音色 / 超时长 / 截断质检），那些根本没发出去，不该
    也没法发布。所以判据收紧成：从 ``sent = await send_telegram_voice(`` 起到其后
    第一个删除为止的那段窗口里，必须出现发布调用。
    """
    src = _sender_src()
    send_at = src.index("sent = await send_telegram_voice(")
    unlink_at = src.index("os.unlink(result.audio_path)", send_at)
    window = src[send_at:unlink_at]
    assert "_publish_media_ref(result.audio_path)" in window, (
        "发布不在「发送 → 删除」窗口内：要么排到了删除之后（拿不到文件），"
        "要么根本没接")


def test_voice_mirrors_use_the_shared_preview_helper():
    """两条语音路径都必须走同一个文案函数，别各写各的（口径分裂是这次问题的根源）。"""
    src = _sender_src()
    assert src.count("_voice_mirror_preview(") >= 3  # 1 定义 + 2 调用
    assert '"[语音]")' not in src, "不该再有裸占位镜像"


# ─────────────────── 分条语音逐条镜像（2026-08-02）───────────────────

def _split_fn_src() -> str:
    src = _sender_src()
    i = src.index("async def _send_voice_reply_parts")
    j = src.index("async def ", i + 10)
    return src[i:j]


def test_split_voice_publishes_each_part_between_send_and_unlink():
    """分条路径：每条的归档发布必须夹在「真发」与「删文件」之间（发完即删）。"""
    body = _split_fn_src()
    send_at = body.index("sent_msg = await send_telegram_voice(")
    unlink_at = body.index("os.unlink(rv.audio_path)", send_at)
    window = body[send_at:unlink_at]
    assert "_publish_media_ref(rv.audio_path)" in window, (
        "分条发布不在「发送 → 删除」窗口内：要么排到删除之后（文件已没了），"
        "要么根本没接——那就退回了「[语音]×N 永远无音频」的旧缺口")


def test_split_voice_mirrors_per_part_not_aggregate():
    """分条路径必须逐条 _mirror_out_row（每条自己的 msg_id/念稿/media_ref），
    contacts 走循环外的一次 _record_contact_out——合并一行的旧口径不得回潮。"""
    body = _split_fn_src()
    assert "_mirror_out_row(" in body, "分条应逐条镜像"
    assert "_record_contact_out(" in body, "contacts 一轮一次"
    # 旧合并口不得再出现在分条函数里（防回退）
    assert "_postsend_mirror_and_record(" not in body


def test_single_voice_mirror_uses_clean_transcript_and_sender():
    """单条语音：镜像行传干净念稿 _vclean（媒体行不再带 [语音] 前缀）+ 人设名
    sender_name（P1-3「谁的音色」）；contacts 预览仍由 _voice_mirror_preview 出
    （带标记）；发布失败也如实标 media_type=voice；send_text_summary 分支的语音行
    本体单独镜像（不再隐形）。"""
    src = _sender_src()
    assert '_vclean = " ".join(str(reply_text or "").split())' in src
    i = src.index('_vclean = " ".join')
    seg = src[i:i + 2600]
    assert seg.count("_mirror_out_row(") >= 2           # summary 分支 + 仅语音分支
    assert seg.count("sender_name=_vwho") >= 2          # 两分支都带人设名
    assert "_voice_mirror_preview(reply_text)" in seg   # contacts 预览带标记
    assert 'media_type=_vmt or "voice"' in seg          # 发布失败仍如实标 voice
