# -*- coding: utf-8 -*-
"""P0 2026-08-19「乱码识图」事故三件套门禁。

事故：客户发来文字密集的图（机械键盘 + 屏幕上的看板/界面），旧生产 vision prompt 的
「逐字抄录」条款让 VLM 把键帽（F2 F3 QWERTY 3# 4$）、看板卡片标题、界面话术整段抄进
``[图片内容]`` 描述 → 坐席端识别行 + 自动译文行两面文字墙，AI 上下文/持久化翻译被污染。

三层修复的服务端不变量（前端 _xlateSrcText/_aiDescRow 折叠由模板门禁+浏览器门禁守）：
① ``desc_looks_garbled``：碎片汤兜底闸门——**保守**判定，票据逐字抄录绝不误杀；
② ``strip_media_desc_for_translation``：媒体消息「可译源文」＝客户 caption 单一口径；
③ ``enrich_inbound_translations``：纯媒体消息（无 caption）不再送翻译引擎并落库。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.inbox.media_enrich import (
    GARBLED_DESC_NOTE,
    desc_looks_garbled,
    enrich_inbound_media_text,
    parse_desc_type,
    strip_media_desc,
)

# 按事故实录重建的碎片汤（键帽 + 看板卡片 + 界面话术混拼）
SOUP = (
    "画面中可见：机械键盘、显示器。键帽文字：F2 F3 F4 F5 F6 F7 F8 F9 F10 F11 "
    "3# 4$ 5% 6^ 7& 8* 9( 0) - = QWERTYUIOP{}[] ASDFGHJKL ZXCVBNM<>? Alt Fn "
    "屏幕上：团队话术 21 问候 你好~ 常答 好的，我马上回复您~ "
    "Candies Feature removal advice 12h 右侧提示悬浮优化 12h 功能改置提议 12h "
    "Chat translation features 12h More Flexible"
)

# 票据逐字抄录（新 prompt A 类的正常产出）——闸门绝不许吃掉它
RECEIPT = (
    "铁路电子客票 E-Ticket 姓名: 张三 (ZHANG SAN) 车次: G1234 日期: 2026-08-21 "
    "座位: 12车05A 票价: ¥553.00 订单号: EK2026082112345 取票码: QRD881 "
    "电话: 13800138000 这是一张铁路电子客票，显示乘客信息、车次、日期与票价。"
)


class TestDescLooksGarbled:
    def test_incident_soup_fires(self):
        assert desc_looks_garbled(SOUP) is True

    def test_keyboard_rows_alone_fire(self):
        # ≥80 字的键帽逐字罗列（无功能键也算）——事故同款模式
        txt = ("画面里有一把机械键盘，按键从左到右依次是 QWERTYUIOP{}[]、ASDFGHJKL:'、"
               "ZXCVBNM<>?，还有空格键和 Alt Ctrl Shift Caps Tab Esc，键帽是橙黑双拼配色。")
        assert desc_looks_garbled(txt) is True

    def test_short_coherent_mention_of_rows_not_gated(self):
        # <80 字的连贯句提到键盘行不算汤——短描述没有刷屏危害且信息可读，
        # 80 字下限是刻意的（闸门哲学：宁可放过，不可误杀）。
        assert desc_looks_garbled("键盘上有 QWERTYUIOP 和 ASDFGHJKL 两行按键。") is False

    def test_receipt_verbatim_never_gated(self):
        assert desc_looks_garbled(RECEIPT) is False

    def test_short_desc_never_gated(self):
        assert desc_looks_garbled("一张机械键盘的照片，桌旁有显示器。") is False
        assert desc_looks_garbled("") is False
        assert desc_looks_garbled(GARBLED_DESC_NOTE) is False  # 提示文案自身不递归命中

    def test_normal_long_chinese_desc_not_gated(self):
        txt = ("用户阿龙在工作台里和客服聊天，讨论团队战术和功能优化建议，"
               "最后一条消息问「你看看这里都有啥」。界面左侧是会话列表，右侧是业务助手面板，"
               "顶部有一条蓝色的系统横幅提示。整体是一个深色主题的客服软件截图。")
        assert desc_looks_garbled(txt) is False

    def test_english_prose_not_gated(self):
        txt = ("The customer is asking about the delivery time for the order and wants "
               "to know whether we can ship it earlier than planned because they will "
               "travel to Singapore next week and need it before the trip starts.")
        assert desc_looks_garbled(txt) is False


class TestStripMediaDesc:
    def test_caption_kept_desc_dropped(self):
        assert strip_media_desc("看这个\n[图片内容] 一只橘猫") == "看这个"

    def test_media_only_returns_empty(self):
        assert strip_media_desc("[图片内容] " + SOUP) == ""
        assert strip_media_desc("[视频内容] 画面：海边") == ""

    def test_plain_text_unchanged(self):
        assert strip_media_desc("你好在吗") == "你好在吗"
        assert strip_media_desc("") == ""

    def test_voice_transcript_unchanged(self):
        # 语音转写没有标记＝对方说的话，必须原样可译
        assert strip_media_desc("明天下午三点可以吗") == "明天下午三点可以吗"

    def test_voice_transcript_marker_stripped(self):
        # P0-V：行首 [语音转录] 数据标记剥离——6 个 CJK 字会把短外语转写判成 zh，
        # 撞 zh 目标语 identity 后该行永远得不到自动译文（FYI 移交项，两端同口径）
        assert strip_media_desc("[语音转录] Selamat pagi") == "Selamat pagi"
        assert strip_media_desc("[语音转录] 明天见") == "明天见"
        # 正文里出现同字样=客户的话，不动；裸标记剥成空串（无可译内容）
        assert strip_media_desc("他发的[语音转录]是什么意思") == "他发的[语音转录]是什么意思"
        assert strip_media_desc("[语音转录] ") == ""

    def test_voice_marker_frontend_mirror(self):
        # 前端 _xlateSrcText 必须同口径剥行首标记（\u8bed\u97f3\u8f6c\u5f55=语音转录）
        from pathlib import Path
        html = (Path(__file__).resolve().parent.parent
                / "src/web/templates/unified_inbox.html").read_text(encoding="utf-8")
        seg = html[html.index("function _xlateSrcText"):]
        seg = seg[:seg.index("function ", 10)]
        assert "\\u8bed\\u97f3\\u8f6c\\u5f55" in seg

    def test_legacy_alias_still_works(self):
        # 同日改名的兼容别名（防并行线引用旧名）
        from src.inbox.media_enrich import strip_media_desc_for_translation
        assert strip_media_desc_for_translation is strip_media_desc


class TestParseDescType:
    """P1 首行类型标记：``类型=A|B|C`` → (代码, 去标记正文)；无标记原样返回。"""

    def test_basic_marker(self):
        assert parse_desc_type("类型=A\n姓名: 张三") == ("A", "姓名: 张三")
        assert parse_desc_type("类型=C\n一张机械键盘的照片。") == ("C", "一张机械键盘的照片。")

    def test_fullwidth_and_case_tolerated(self):
        assert parse_desc_type("类型＝b\n对方在和客服聊退款")[0] == "B"
        assert parse_desc_type("type: a\nName: ZHANG SAN")[0] == "A"

    def test_no_marker_returns_original(self):
        assert parse_desc_type("一张机械键盘的照片。") == ("", "一张机械键盘的照片。")
        assert parse_desc_type("") == ("", "")

    def test_marker_mid_text_not_stripped(self):
        # 标记只认首行——正文里出现「类型=」字样不得误剥
        t = "这是一份表格，第三列写着 类型=A 的字样。"
        assert parse_desc_type(t) == ("", t)

    def test_marker_only_no_body(self):
        assert parse_desc_type("类型=C") == ("C", "")


@pytest.mark.asyncio
async def test_enrich_gates_garbled_vision_output(tmp_path):
    """识别层端到端：VLM 吐碎片汤 → 消息文本换成诚实提示 + 计 miss(garbled)。"""
    from src.inbox.media_enrich_stats import get_media_enrich_stats

    img = tmp_path / "a.jpg"
    img.write_bytes(b"fakejpg")
    st = get_media_enrich_stats()
    st.reset()
    with patch(
        "src.inbox.media_enrich._resolve_local_path", return_value=str(img),
    ), patch(
        "src.vision_client.has_any_vision_backend", return_value=True,
    ), patch(
        "src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
        new=AsyncMock(return_value=(SOUP, "ollama_ok")),
    ):
        text, desc = await enrich_inbound_media_text(
            media_type="image", media_ref="/static/protocol_media/whatsapp/a.jpg",
            caption="", config={"vision": {"enabled": True}},
        )
    assert desc == GARBLED_DESC_NOTE
    assert text == f"[图片内容] {GARBLED_DESC_NOTE}"
    assert get_media_enrich_stats().dump()["by_reason"].get("garbled") == 1


@pytest.mark.asyncio
async def test_enrich_counts_desc_type_distribution(tmp_path):
    """P1：带类型标记的识别产出 → 标记保留在文本里（结构载体）+ 计入 by_type 分布。"""
    from src.inbox.media_enrich_stats import get_media_enrich_stats

    img = tmp_path / "a.jpg"
    img.write_bytes(b"fakejpg")
    st = get_media_enrich_stats()
    st.reset()
    typed = "类型=A\n姓名: 张三 单号: EK2026082112345"
    with patch(
        "src.inbox.media_enrich._resolve_local_path", return_value=str(img),
    ), patch(
        "src.vision_client.has_any_vision_backend", return_value=True,
    ), patch(
        "src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
        new=AsyncMock(return_value=(typed, "ollama_ok")),
    ):
        text, desc = await enrich_inbound_media_text(
            media_type="image", media_ref="/static/protocol_media/whatsapp/a.jpg",
            caption="", config={"vision": {"enabled": True}},
        )
    assert desc == typed                      # 标记保留（前端剥离显示、AI 可见分类信号）
    assert text == f"[图片内容] {typed}"
    assert get_media_enrich_stats().dump()["by_type"].get("A") == 1


def test_memory_extraction_strips_media_desc_wiring():
    """接线钉：两条记忆抽取链必须剥识别描述（防「截图内容变用户事实」）。

    静态源码断言（构造 AIClient/SkillManager 需要重依赖，wiring 被移除时这里先红）。
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    ai_src = (root / "ai" / "ai_client.py").read_text(encoding="utf-8")
    sm_src = (root / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    # ai_client.extract_memory_bullets 入口剥离（单一收口，覆盖全部调用方）
    seg = ai_src.split("async def extract_memory_bullets", 1)[1][:1200]
    assert "strip_media_desc" in seg, "extract_memory_bullets 丢失媒体描述剥离"
    # skill_manager 启发式抽取吃的是剥离后的 mu_facts
    assert "extract_heuristic_facts(mu_facts)" in sm_src, "启发式抽取丢失媒体描述剥离"


def test_telegram_a_line_keeps_caption_with_desc():
    """接线钉（2026-08-21 值守事故）：Telegram A 线图片消息 caption 必须保留。

    此前 telegram_client 把 text **整个替换**成「[图片内容] 描述」——用户带
    文字的截图报障（「发送图片，报错，分析问题」）正文被丢，AI/bug_intake
    观察层/收件箱镜像只见描述，支持号对着截图里的客户对话接闲聊（内测群
    02:54-03:18 实录）。正确口径与 LINE 线 / enrich_media_text 一致：
    caption 在前、描述在后。静态源码断言（构造 TelegramClient 重依赖）。
    """
    import pathlib
    tg_src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "client"
              / "telegram_client.py").read_text(encoding="utf-8")
    seg = tg_src.split('if _ocr and _media_type == "image"', 1)[1][:1200]
    assert 'f"{_cap}\\n[图片内容] {_ocr}"' in seg, "caption 保留逻辑被移除"
    assert 'text = f"[图片内容] {_ocr}"' not in seg, "裸替换写法复活（丢 caption）"


@pytest.mark.asyncio
async def test_enrich_keeps_receipt_verbatim(tmp_path):
    """票据逐字抄录必须原样通过闸门（业务刚需不回退）。"""
    img = tmp_path / "a.jpg"
    img.write_bytes(b"fakejpg")
    with patch(
        "src.inbox.media_enrich._resolve_local_path", return_value=str(img),
    ), patch(
        "src.vision_client.has_any_vision_backend", return_value=True,
    ), patch(
        "src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
        new=AsyncMock(return_value=(RECEIPT, "ollama_ok")),
    ):
        text, desc = await enrich_inbound_media_text(
            media_type="image", media_ref="/static/protocol_media/whatsapp/a.jpg",
            caption="", config={"vision": {"enabled": True}},
        )
    assert desc == RECEIPT
    assert text == f"[图片内容] {RECEIPT}"


class TestImageUnderstandReport:
    """周读 CLI 纯函数（DB 口径观测——进程计数重启即清零，库口径跨重启恒真）。"""

    def test_extract_desc_and_analyze(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from image_understand_report import analyze_rows, extract_desc

        assert extract_desc("看这个\n[图片内容] 类型=A\n姓名: 张三") == "类型=A\n姓名: 张三"
        assert extract_desc("你好在吗") == ""
        rows = [
            {"text": "[图片内容] 类型=A\n姓名: 张三 单号: E1"},
            {"text": "[图片内容] 类型=C\n一张机械键盘的照片。"},
            {"text": "[图片内容] " + GARBLED_DESC_NOTE},          # 闸门替换文案
            {"text": "[图片内容] " + SOUP},                        # 残余汤（存量漏网）
            {"text": "[图片] "},                                   # 无描述（占位）
            {"text": "[视频内容] 画面：海边散步"},                  # 未标记（旧产出）
        ]
        rep = analyze_rows(rows)
        assert rep["inbound_media_msgs"] == 6
        assert rep["with_desc"] == 5
        assert rep["by_type"] == {"A": 1, "B": 0, "C": 1, "untyped": 3}
        assert rep["garbled_gated"] == 1
        assert rep["soup_residual"] == 1
        assert rep["desc_len_p50"] > 0


def _fake_req(store=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(inbox_store=store)))


def _fake_cfgm(target="zh"):
    return SimpleNamespace(config={
        "workspace": {"auto_translate_inbound": {"enabled": True, "target_lang": target}},
    })


@pytest.mark.asyncio
async def test_inbound_translate_skips_media_desc_only():
    """服务端持久化翻译：纯媒体消息（text 只有识别描述）绝不送引擎。"""
    from src.workspace.inbound_translate import enrich_inbound_translations

    svc = SimpleNamespace(
        _router=SimpleNamespace(any_available=lambda: True),
        translate=AsyncMock(side_effect=AssertionError("纯媒体消息不应触发翻译")),
    )
    msgs = [{"direction": "in", "message_id": "m1",
             "text": "[图片内容] " + SOUP, "ts": 100}]
    out, stats = await enrich_inbound_translations(
        _fake_req(), msgs, conversation_id="c1",
        config_manager=_fake_cfgm(), translation_svc=svc,
    )
    assert stats["translated"] == 0 and stats["failed"] == 0
    assert stats["skipped"] >= 1
    svc.translate.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_translate_uses_caption_only():
    """带 caption 的媒体消息：送引擎的源文必须是 caption，不含识别描述。"""
    from src.workspace.inbound_translate import enrich_inbound_translations

    class _Res:
        ok = True
        translated_text = "看看这个"
        source_lang = "fr"

        def to_dict(self):
            return {"ok": True, "target_lang": "zh",
                    "translated_text": self.translated_text}

    svc = SimpleNamespace(
        _router=SimpleNamespace(any_available=lambda: True),
        translate=AsyncMock(return_value=_Res()),
    )
    msgs = [{"direction": "in", "message_id": "m2", "language": "fr",
             "text": "Regarde ça\n[图片内容] 一张机械键盘的照片", "ts": 101}]
    out, stats = await enrich_inbound_translations(
        _fake_req(), msgs, conversation_id="c1",
        config_manager=_fake_cfgm(), translation_svc=svc,
    )
    assert stats["translated"] == 1
    sent_text = svc.translate.await_args[0][0]
    assert sent_text == "Regarde ça"
    assert "[图片内容]" not in sent_text
    assert out[0]["translated_text"] == "看看这个"
