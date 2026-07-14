"""inbound_enrich — 收件箱入站上下文补全单测。"""

from src.inbox.inbound_enrich import (
    apply_inbound_enrichments,
    build_language_switch_hint,
    build_short_inbound_hint,
    peer_media_context,
)


def test_peer_media_sticker_placeholder():
    ctx = peer_media_context("[贴纸]")
    assert ctx["_peer_message_is_media"] is True
    assert ctx["_media_kind"] == "sticker"


def test_peer_media_image_content_desc():
    ctx = peer_media_context("[图片内容] 宝宝在笑", media_type="image")
    assert ctx["_media_desc"] == "宝宝在笑"


# ── 阶段 3：全平台占位格式统一解析（Messenger/LINE/TG A 线 → 结构化媒体上下文）──

def test_peer_media_messenger_image_with_desc():
    """Messenger to_text_for_ai 的 `[图片] desc` → 结构化 kind+desc（此前不触发媒体块）。"""
    ctx = peer_media_context("[图片] 一只橘猫趴在键盘上")
    assert ctx["_peer_message_is_media"] is True
    assert ctx["_media_kind"] == "image"
    assert ctx["_media_desc"] == "一只橘猫趴在键盘上"


def test_peer_media_messenger_sticker_with_category():
    ctx = peer_media_context("[贴纸·happy] 开心大笑的柴犬")
    assert ctx["_media_kind"] == "sticker"
    assert ctx["_media_desc"] == "开心大笑的柴犬"


def test_peer_media_messenger_animated_sticker_with_category():
    ctx = peer_media_context("[动态贴纸·love] 爱心熊举牌")
    assert ctx["_media_kind"] == "animated_sticker"
    assert ctx["_media_desc"] == "爱心熊举牌"


def test_peer_media_messenger_video_and_bare_gif():
    ctx = peer_media_context("[视频] 海边日落缩略图，0:15")
    assert ctx["_media_kind"] == "video"
    assert ctx["_media_desc"] == "海边日落缩略图，0:15"
    ctx2 = peer_media_context("[GIF]")
    assert ctx2["_media_kind"] == "gif"
    assert "_media_desc" not in ctx2


def test_peer_media_line_sticker_and_image():
    ctx = peer_media_context("[LINE贴图] 棕熊欢呼撒彩纸")
    assert ctx["_media_kind"] == "sticker"
    assert ctx["_media_desc"] == "棕熊欢呼撒彩纸"
    ctx2 = peer_media_context("[图片消息] 键盘快捷键列表截图")
    assert ctx2["_media_kind"] == "image"
    assert ctx2["_media_desc"] == "键盘快捷键列表截图"


def test_peer_media_line_video_and_gif_kinds():
    """LINE 新增 video/gif kind（此前 LINE 视频完全未建模）。"""
    ctx = peer_media_context("[视频消息] 海边日落缩略图，0:15")
    assert ctx["_media_kind"] == "video"
    ctx2 = peer_media_context("[动图消息] 小猫疯狂点头")
    assert ctx2["_media_kind"] == "gif"
    assert ctx2["_media_desc"] == "小猫疯狂点头"


def test_peer_media_tg_emoji_annotation():
    """TG A 线 `[表情] …`（emoji demojize / 贴纸 Vision）→ sticker 语境（此前 enrich 不认）。"""
    ctx = peer_media_context("[表情] 笑哭了")
    assert ctx["_media_kind"] == "sticker"
    assert ctx["_media_desc"] == "笑哭了"


def test_peer_media_tg_video_content():
    """TG `[视频内容] …`（抽帧+ASR 产物）→ video + 描述（此前 enrich 不解析）。"""
    ctx = peer_media_context("[视频内容] 画面：海边奔跑 语音：明天见")
    assert ctx["_media_kind"] == "video"
    assert "海边奔跑" in ctx["_media_desc"]


def test_peer_media_fusion_hint_stripped():
    """Messenger fusion 提示行不得混进 desc。"""
    ctx = peer_media_context("[图片] 一只猫\n[上下文提示] 上一句 peer 说'看看我的照片'")
    assert ctx["_media_kind"] == "image"
    assert ctx["_media_desc"] == "一只猫"


def test_peer_media_link_is_not_media():
    assert peer_media_context("[链接] 新闻标题 http://x.example") == {}


def test_peer_media_plain_text_no_patch():
    assert peer_media_context("你好呀，在忙吗") == {}


def test_peer_media_explicit_fields_win_over_text():
    """结构化来源（WA runner / 收件箱行）优先于文本解析。"""
    ctx = peer_media_context(
        "[图片] 文本里的描述", media_type="video", media_desc="显式描述",
    )
    assert ctx["_media_kind"] == "video"
    assert ctx["_media_desc"] == "显式描述"


def test_language_switch_hint_en_to_ja():
    hist = [
        {"role": "user", "content": "How are you today?"},
        {"role": "assistant", "content": "Hey, good!"},
    ]
    hint = build_language_switch_hint(
        hist, current_lang="ja", current_text="私も悪くないよ",
    )
    assert "日语" in hint
    assert "英语" in hint


def test_language_switch_hint_chinese_text_never_false_english():
    """真机 bug 回归：用户说中文，但传入 current_lang 被上一轮锁成 en（陈旧）——
    绝不能提示"突然换成英语啦"。以文本实际语种为准，中文 → 空提示。"""
    hist = [
        {"role": "user", "content": "你在干嘛呢"},
        {"role": "assistant", "content": "在刷手机呀"},
    ]
    hint = build_language_switch_hint(
        hist, current_lang="en", current_text="这个时候你在干嘛呢",
    )
    assert hint == ""


def test_language_switch_hint_conflict_prefers_text():
    """current_lang 与文本矛盾时以文本为准：文本英文但 current_lang=zh → 仍按英文判断。"""
    hist = [{"role": "user", "content": "你好呀最近怎么样"}]
    hint = build_language_switch_hint(
        hist, current_lang="zh", current_text="hey what are you up to tonight",
    )
    assert "英语" in hint  # 文本是英文 → 相对中文历史，应提示切英语


def test_short_inbound_hint_interjection():
    assert "语气词" in build_short_inbound_hint("嗯嗯")


def test_apply_inbound_enrichments_sets_media_and_short():
    uc: dict = {}
    apply_inbound_enrichments(
        uc, text="Hi", history=[], reply_lang="en", platform="telegram",
    )
    assert uc["last_message"] == "Hi"
    assert uc["_current_user_message_for_lang"] == "Hi"
    assert "极短英文" in uc.get("_inbound_short_hint", "")


def test_apply_sticker_media_patch():
    uc: dict = {}
    apply_inbound_enrichments(uc, text="[贴纸]", platform="telegram")
    assert uc["_peer_message_is_media"] is True
    assert uc["_media_kind"] == "sticker"


def test_apply_messenger_style_text_end_to_end():
    """端到端：Messenger 风格 `[图片] desc` 经 apply_inbound_enrichments 出结构化媒体上下文，
    且 last_message 保持原文不被改写。"""
    uc: dict = {}
    apply_inbound_enrichments(uc, text="[图片] 一只橘猫趴在键盘上", platform="messenger")
    assert uc["_peer_message_is_media"] is True
    assert uc["_media_kind"] == "image"
    assert uc["_media_desc"] == "一只橘猫趴在键盘上"
    assert uc["last_message"] == "[图片] 一只橘猫趴在键盘上"
