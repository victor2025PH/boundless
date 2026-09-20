# -*- coding: utf-8 -*-
"""Phase1.3 识图对齐 + 图文连发补识 行为回归（enrich_auto_draft 媒体链）。

覆盖：
① 最新入站是裸图/带 caption 图 → 识别结果并入待回复正文与 history
   （对齐 Telegram A 线与本文件语音/视频分支的既有口径）；
② 「先发图/语音紧跟一句话」→ 向前回扫补识旧媒体行：回写消息行 + 精确替换
   history 对应占位（连发多图也不错行）+ 描述照带 media_desc 进产线；
③ 已识别/带 caption 的行不重复识别；识别失败软降级不阻塞拟稿（旧行为回落）；
④ 回扫窗口 inbox.auto_draft.media_backscan 可配：≤1 退化旧行为、坏值回默认；
⑤ 语音最新入站：转写替换 last 的既有语义不回归。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.inbox.autodraft_helpers import enrich_auto_draft


def _row(direction, text="", media_type="", media_ref="", mid="", ts=0):
    return {
        "direction": direction, "text": text, "media_type": media_type,
        "media_ref": media_ref, "message_id": mid, "ts": ts,
    }


class _Store:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def list_recent_messages(self, cid, limit=30):
        return list(self._rows)

    def update_message_text(self, cid, message_id="", media_ref="",
                            text="", only_if_empty=False):
        self.updates.append({
            "message_id": message_id, "media_ref": media_ref,
            "text": text, "only_if_empty": only_if_empty,
        })
        return True


class _Cfg:
    """assistant.config 替身：真 dict 语义（MagicMock.get 会返回 truthy mock，
    误入 SER 等旁路分支）。"""

    def __init__(self, data=None):
        self.config = data or {}

    def get(self, key, default=None):
        return self.config.get(key, default)


def _assistant(cfg=None):
    a = MagicMock()
    a.config = _Cfg(cfg)
    return a


def _run(rows, *, image_desc=None, backfill=None, text="hi",
         cfg=None, voice_text=None):
    """驱动 enrich_auto_draft，返回 (生成引擎捕获的 kwargs, store, 补识 mock)。"""
    store = _Store(rows)
    draft_svc = MagicMock()
    draft_svc.get_draft.return_value = {}
    draft_svc.enrich_draft.return_value = True
    app = MagicMock()
    tc = MagicMock()
    tc._get_image_content = AsyncMock(return_value=image_desc)
    if voice_text is not None:
        vtr = MagicMock()
        vtr.transcribe_voice_message = AsyncMock(return_value=voice_text)
        tc.voice_transcriber = vtr
    else:
        tc.voice_transcriber = None
    app.state.telegram_client = tc
    captured = {}

    async def _fake_gen(**kw):
        captured.update(kw)
        return {"ok": True, "reply": "好可爱", "reply_lang": "zh"}

    conv = {"conversation_id": "whatsapp:acc:peer", "platform": "whatsapp",
            "chat_key": "peer", "account_id": "acc"}
    bf = AsyncMock(return_value=(backfill or ("", "")))
    with patch("src.inbox.persona_reply.generate_persona_reply", _fake_gen), \
         patch("src.integrations.protocol_bridge.static_media_ref_to_path",
               return_value="C:/tmp/fake.jpg"), \
         patch("src.inbox.media_enrich.enrich_inbound_media_text", bf):
        asyncio.run(enrich_auto_draft(
            _assistant(cfg), draft_svc, app, store, conv, text, "d1",
            "review"))
    return captured, store, bf


def test_latest_bare_image_folds_desc_into_last():
    rows = [
        _row("in", "看看这个", mid="m1", ts=100),
        _row("out", "好呀", mid="m2", ts=101),
        _row("in", "", media_type="image",
             media_ref="/static/protocol_media/whatsapp/a.jpg",
             mid="m3", ts=102),
    ]
    kw, store, _bf = _run(rows, image_desc="一只橘猫趴在沙发上")
    assert kw["last_inbound"] == "[图片内容] 一只橘猫趴在沙发上"
    users = [h for h in kw["history"] if h["role"] == "user"]
    assert users[-1]["content"] == "[图片内容] 一只橘猫趴在沙发上"
    assert kw["media_desc"] == "一只橘猫趴在沙发上"
    assert kw["media_type"] == "image"
    assert any(
        u["message_id"] == "m3" and u["text"].startswith("[图片内容]")
        and u["only_if_empty"] for u in store.updates
    )


def test_latest_image_with_caption_keeps_caption_and_desc():
    rows = [
        _row("in", "这是我家猫", media_type="image",
             media_ref="/static/x.jpg", mid="m1", ts=100),
    ]
    kw, _store, _bf = _run(rows, image_desc="橘猫")
    assert kw["last_inbound"] == "这是我家猫\n[图片内容] 橘猫"
    users = [h for h in kw["history"] if h["role"] == "user"]
    assert users[-1]["content"] == "这是我家猫\n[图片内容] 橘猫"


def test_image_then_text_backfills_older_image():
    rows = [
        _row("in", "", media_type="image", media_ref="/static/a.jpg",
             mid="m1", ts=100),
        _row("in", "这是我家猫 可爱吧", mid="m2", ts=101),
    ]
    kw, store, bf = _run(
        rows, backfill=("[图片内容] 一只橘猫", "一只橘猫"))
    # 待回复正文保持客户原话，peer 媒体语义不变（media_type 忠实于「最新是文字」）
    assert kw["last_inbound"] == "这是我家猫 可爱吧"
    assert kw["media_type"] == ""
    # history 里旧图占位被替换成识别结果
    users = [h for h in kw["history"] if h["role"] == "user"]
    assert users[0]["content"] == "[图片内容] 一只橘猫"
    assert users[-1]["content"] == "这是我家猫 可爱吧"
    assert bf.await_count == 1
    assert any(u["message_id"] == "m1" for u in store.updates)
    # 描述照带 media_desc 进产线，并附「媒体是之前那条」注记
    assert kw["media_desc"].startswith("一只橘猫")
    assert "之前" in kw["media_desc"] and "图片" in kw["media_desc"]


def test_backfill_skips_already_enriched_rows():
    rows = [
        _row("in", "[图片内容] 旧描述", media_type="image",
             media_ref="/static/a.jpg", mid="m1", ts=100),
        _row("in", "在吗", mid="m2", ts=101),
    ]
    _kw, store, bf = _run(rows)
    assert bf.await_count == 0
    assert not store.updates


def test_two_bare_images_backfill_patches_correct_history_row():
    # 最新入站也是裸图但识别失败（desc=None）；更早一张由补识补上——
    # history 必须精确替换旧行，不误伤最新占位行。
    # （08-17 无兜底纪律后：识别失败要放行拟稿需 media_degrade_reply 开，
    #   否则 vision_hold 取消——本例关注 history 替换语义，故开降级。）
    rows = [
        _row("in", "", media_type="image", media_ref="/static/old.jpg",
             mid="m1", ts=100),
        _row("out", "嗯嗯", mid="m2", ts=101),
        _row("in", "", media_type="image", media_ref="/static/new.jpg",
             mid="m3", ts=102),
    ]
    kw, _store, _bf = _run(
        rows, image_desc=None,
        backfill=("[图片内容] 一杯咖啡", "一杯咖啡"),
        cfg={"inbox": {"auto_draft": {"media_degrade_reply": True}}})
    users = [h for h in kw["history"] if h["role"] == "user"]
    assert users[0]["content"] == "[图片内容] 一杯咖啡"
    assert users[-1]["content"] == "[图片]"  # 最新失败行保持占位
    assert kw["last_inbound"] == "[图片]"
    # 最新入站本身是媒体行（识别失败）→ 不把旧图描述混进 media_desc
    assert kw["media_desc"] == ""


def test_vision_fail_soft_degrades():
    """识别失败 + media_degrade_reply 开 → 降级诚实拟稿（占位喂产线，不扣留）。

    （08-17 无兜底纪律后本例语义更新：降级放行是 opt-in；默认关走 vision_hold
    取消，见 test_vision_fail_default_holds_draft。）"""
    rows = [
        _row("in", "", media_type="image", media_ref="/static/a.jpg",
             mid="m1", ts=100),
    ]
    kw, store, bf = _run(
        rows, image_desc=None,
        cfg={"inbox": {"auto_draft": {"media_degrade_reply": True}}})
    assert kw["last_inbound"] == "[图片]"
    assert kw.get("media_desc", "") == ""
    # 拟稿仍完成（生成引擎被调用）；最新失败行不在同稿内重复重试
    assert kw.get("last_inbound") is not None
    assert bf.await_count == 0


def test_vision_fail_default_holds_draft():
    """识别失败 + 降级关（默认）→ 08-17 无兜底纪律：取消拟稿不生成。"""
    rows = [
        _row("in", "", media_type="image", media_ref="/static/b.jpg",
             mid="m1", ts=100),
    ]
    kw, _store, _bf = _run(rows, image_desc=None)
    assert kw == {}  # 生成引擎未被调用（vision_hold 在生成之前拦下）


def test_backfill_recognition_fail_keeps_old_behavior():
    """回扫识别失败（无 vision/空描述）→ 完全回落旧行为：占位不动、零回写。"""
    rows = [
        _row("in", "", media_type="image", media_ref="/static/a.jpg",
             mid="m1", ts=100),
        _row("in", "在吗", mid="m2", ts=101),
    ]
    kw, store, bf = _run(rows, backfill=("[图片]", ""))
    assert bf.await_count == 1
    assert not store.updates
    users = [h for h in kw["history"] if h["role"] == "user"]
    assert users[0]["content"] == "[图片]"
    assert kw["last_inbound"] == "在吗"
    assert kw["media_desc"] == ""


def test_backscan_le_one_degrades_to_old_behavior():
    """media_backscan ≤1 → 关闭回扫：旧媒体行不识别、不回写、history 保持占位。"""
    rows = [
        _row("in", "", media_type="image", media_ref="/static/a.jpg",
             mid="m1", ts=100),
        _row("in", "这是我家猫 可爱吧", mid="m2", ts=101),
    ]
    kw, store, bf = _run(
        rows, cfg={"inbox": {"auto_draft": {"media_backscan": 1}}},
        backfill=("[图片内容] 一只橘猫", "一只橘猫"))
    assert bf.await_count == 0
    assert not store.updates
    assert kw["last_inbound"] == "这是我家猫 可爱吧"
    users = [h for h in kw["history"] if h["role"] == "user"]
    assert users[0]["content"] == "[图片]"
    assert kw["media_desc"] == ""


def test_backscan_window_bounds_scan():
    """窗口按「最近 N 条消息」计：更早的媒体行落在窗口外 → 不识别。"""
    rows = [
        _row("in", "", media_type="image", media_ref="/static/a.jpg",
             mid="m1", ts=100),
        _row("out", "嗯嗯", mid="m2", ts=101),
        _row("in", "在吗", mid="m3", ts=102),
    ]
    _kw, store, bf = _run(
        rows, cfg={"inbox": {"auto_draft": {"media_backscan": 2}}},
        backfill=("[图片内容] x", "x"))
    assert bf.await_count == 0
    assert not store.updates


def test_backscan_int_guard_and_string_value():
    """坏值回默认 5（照常回扫）；字符串数字被 int() 收编。"""
    rows = [
        _row("in", "", media_type="image", media_ref="/static/a.jpg",
             mid="m1", ts=100),
        _row("in", "看下这个", mid="m2", ts=101),
    ]
    # 坏值 "abc" → 默认 5 → 回扫生效
    kw, store, bf = _run(
        rows, cfg={"inbox": {"auto_draft": {"media_backscan": "abc"}}},
        backfill=("[图片内容] 合同截图", "合同截图"))
    assert bf.await_count == 1
    users = [h for h in kw["history"] if h["role"] == "user"]
    assert users[0]["content"] == "[图片内容] 合同截图"
    # 字符串 "3" → int 3 → 回扫生效
    kw2, _s2, bf2 = _run(
        rows, cfg={"inbox": {"auto_draft": {"media_backscan": "3"}}},
        backfill=("[图片内容] 合同截图", "合同截图"))
    assert bf2.await_count == 1


def test_voice_latest_transcript_replaces_last():
    """既有语义不回归：最新入站是语音 → 转写替换 last + history 末条同步。"""
    rows = [
        _row("in", "你好", mid="m0", ts=99),
        _row("in", "", media_type="voice", media_ref="/static/v.ogg",
             mid="m1", ts=100),
    ]
    kw, store, bf = _run(rows, voice_text="明天有空吗")
    assert kw["last_inbound"] == "明天有空吗"
    users = [h for h in kw["history"] if h["role"] == "user"]
    assert users[-1]["content"] == "明天有空吗"
    assert kw["media_type"] == "voice"
    assert kw["media_desc"] == "明天有空吗"
    assert any(
        u["message_id"] == "m1" and u["text"] == "明天有空吗"
        and u["only_if_empty"] for u in store.updates
    )
    assert bf.await_count == 0  # 无更早媒体行可补


def test_voice_then_text_backfills_transcript():
    """语音后文字：回扫转写旧语音行，last 保持文字，转写进 history + media_desc。"""
    rows = [
        _row("in", "", media_type="voice", media_ref="/static/v.ogg",
             mid="m1", ts=100),
        _row("in", "咋不回我", mid="m2", ts=101),
    ]
    kw, store, bf = _run(rows, backfill=("明天有空吗", "明天有空吗"))
    assert kw["last_inbound"] == "咋不回我"
    assert kw["media_type"] == ""
    users = [h for h in kw["history"] if h["role"] == "user"]
    assert users[0]["content"] == "明天有空吗"
    assert users[-1]["content"] == "咋不回我"
    assert any(u["message_id"] == "m1" for u in store.updates)
    assert kw["media_desc"].startswith("明天有空吗")
    assert "语音" in kw["media_desc"]
