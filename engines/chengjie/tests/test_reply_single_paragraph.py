"""单段落回复合同门禁（2026-08-08，客户实锤「回复永远两段像 AI」）。

核心不变量：**合同与出口一致**——
  - bubbles 开：拟稿合同=多行（投递层真的按行拆独立消息），出口保留换行但
    **剔除段落间空行**（2026-08-15 拍板「段落之间不要有空行，要链接在一起」）；
  - bubbles 关：拟稿合同=一段话，出口 ``_shape_single_paragraph`` 硬护栏折叠多行。
两侧共用同一判据 ``_single_paragraph_mode``（适用面=``_chat_reply_surface``），
防「合同说一段、出口放两段」分叉；工具型调用（context=None）任何档位零触碰。
"""

from __future__ import annotations

from src.ai.ai_client import AIClient


def _cfg(bubbles_enabled: bool, domain: str = "conversion"):
    class _Cfg:
        config_path = None
        config = {
            "domain": domain,
            "web_admin": {"site_name": "T"},
            "ai": {},
            "inbox": {"reply_style": {"bubbles": {"enabled": bubbles_enabled}}},
        }

        def get_ai_config(self):
            return {}

    return _Cfg()


# ── 拟稿合同随投递能力切换 ─────────────────────────────────────────────────────

def test_contract_single_paragraph_when_bubbles_off():
    client = AIClient(_cfg(bubbles_enabled=False))
    out = client._build_context_prompt({"channel": "telegram"})
    assert "一段话" in out
    assert "一个段落" in out
    # 多行合同绝不能同时在场（那是「两段式」的来源）
    assert "每行会被拆成独立消息" not in out
    assert "绝对禁止把所有话挤在一段里" not in out


def test_contract_multiline_when_bubbles_on():
    client = AIClient(_cfg(bubbles_enabled=True))
    out = client._build_context_prompt({"channel": "telegram"})
    assert "每行会被拆成独立消息" in out
    assert "一段话（最高优先级）" not in out
    # 2026-08-15：多行合同必须从源头禁空行（出口另有 strip_blank_lines 硬护栏）
    assert "不要留空行" in out


def test_contract_absent_outside_chat_surfaces():
    """非陪伴域 + 非 RPA 渠道：两种格式合同都不注入（业务域多行结构合法）。"""
    client = AIClient(_cfg(bubbles_enabled=False, domain="general"))
    out = client._build_context_prompt({"channel": "telegram"})
    assert "回复格式" not in out


def test_contract_rpa_channel_follows_bubbles_even_in_general_domain():
    client = AIClient(_cfg(bubbles_enabled=False, domain="general"))
    out = client._build_context_prompt({"channel": "line_rpa"})
    assert "一段话" in out


# ── 出口硬护栏与合同同判据 ─────────────────────────────────────────────────────

def test_shape_collapses_when_bubbles_off():
    client = AIClient(_cfg(bubbles_enabled=False))
    ctx = {"channel": "telegram"}
    assert client._single_paragraph_mode(ctx) is True
    out = client._shape_single_paragraph("哈哈真的假的\n\n那你后来怎么处理的呀？", ctx)
    assert "\n" not in out
    assert out == "哈哈真的假的，那你后来怎么处理的呀？"


def test_shape_untouched_when_bubbles_on():
    """bubbles 开＝多行是给投递层拆条用的合法形态，出口绝不折叠换行。"""
    client = AIClient(_cfg(bubbles_enabled=True))
    ctx = {"channel": "telegram"}
    assert client._single_paragraph_mode(ctx) is False
    text = "哈哈真的假的\n我还以为你忘了呢"
    assert client._shape_single_paragraph(text, ctx) == text


def test_shape_strips_blank_lines_when_bubbles_on():
    """bubbles 开：换行保留（拆条合同），但段落间空行必须收掉（2026-08-15
    拍板「生成的回复段落之间不要有空行，要链接在一起」——截图实锤形态）。"""
    client = AIClient(_cfg(bubbles_enabled=True))
    ctx = {"channel": "telegram"}
    out = client._shape_single_paragraph(
        "Ah, fair point—the name does give it away.\n\n"
        "But trust me, my Mandarin's rusty.\n\n"
        "What about you—where are you based?", ctx)
    assert "\n\n" not in out
    assert out == (
        "Ah, fair point—the name does give it away.\n"
        "But trust me, my Mandarin's rusty.\n"
        "What about you—where are you based?"
    )


def test_shape_skips_utility_calls_without_context():
    """chat() 工具链 context=None：结构化多行输出（抽取/摘要）绝不被触碰——
    折叠与空行剔除都不许（空行可能是结构化输出的分段语义）。"""
    client = AIClient(_cfg(bubbles_enabled=False))
    text = "line1\nline2\nline3"
    assert client._shape_single_paragraph(text, None) == text
    assert client._single_paragraph_mode(None) is False
    client_on = AIClient(_cfg(bubbles_enabled=True))
    text_blank = "line1\n\nline2"
    assert client_on._shape_single_paragraph(text_blank, None) == text_blank


def test_shape_skips_non_chat_domain():
    client = AIClient(_cfg(bubbles_enabled=False, domain="general"))
    text = "第一行\n第二行"
    assert client._shape_single_paragraph(text, {"channel": "telegram"}) == text
    # 非聊天面（业务域普通渠道）连空行也不动——多行结构合法
    client_on = AIClient(_cfg(bubbles_enabled=True, domain="general"))
    text_blank = "第一段\n\n第二段"
    assert client_on._shape_single_paragraph(text_blank, {"channel": "telegram"}) == text_blank
