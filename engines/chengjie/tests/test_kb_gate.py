"""KB 注入守门（kb_gate）单测——用 2026-07-21 真机事故案例锚定行为。"""

from __future__ import annotations

from src.utils.kb_gate import (
    content_tokens,
    is_media_desc_text,
    lexical_overlap_ok,
    persona_kb_suppressed,
)

# ── 真实事故案例（2026-07-21 夜，WhatsApp 真号实测日志）─────────────────

_USDT_ENTRY = {
    "title": "怎么付款：USDT 结算",
    "triggers": "怎么付款 付款方式 USDT 结算 收款地址",
    "scenario": "客户询问如何付款、用什么结算",
    "category": "商务合作",
}

_INSTALL_ENTRY = {
    "title": "客户端安装指引",
    "triggers": "安装 下载 客户端 教程 怎么装",
    "scenario": "客户问怎么安装客户端",
    "category": "使用帮助",
}

_PRODUCT_ENTRY = {
    "title": "七产品线总览",
    "triggers": "产品 价格 智聊 通译 幻声 介绍 功能",
    "scenario": "客户想了解产品和价格",
    "category": "产品介绍",
}


def test_incident_usdt_bleed_blocked():
    """事故①：「怎麼不恢復我了呢」(8字) 命中 USDT 付款条目(14.8分) → 必须拦。"""
    ok, why = lexical_overlap_ok("怎麼不恢復我了呢", _USDT_ENTRY)
    assert not ok, f"USDT 串台必须被词汇守门拦下 (why={why})"


def test_incident_cantonese_smalltalk_blocked():
    """事故②：「你识唔识讲粤语噶」命中「智聊核心卖点」类条目 → 拦。"""
    entry = {
        "title": "智聊核心卖点：拟人翻译+自动成交+人设语音",
        "triggers": "智聊 卖点 功能 翻译 成交 语音",
        "scenario": "客户问智聊有什么功能优势",
        "category": "产品介绍",
    }
    ok, why = lexical_overlap_ok("你识唔识讲粤语噶？", entry)
    assert not ok, f"粤语闲聊不应命中产品卖点 (why={why})"


def test_legit_product_price_hit_allowed():
    """正例：「帮我介绍一下智聊的价格」→「七产品线总览」(292分) 必须放行。"""
    ok, why = lexical_overlap_ok("帮我介绍一下智聊的价格", _PRODUCT_ENTRY)
    assert ok, f"真实产品咨询必须放行 (why={why})"


def test_media_desc_text_detection():
    """识图/识视频描述文本应整体跳过 KB；语音转写是用户的话，不算。"""
    assert is_media_desc_text("[图片内容] 这张图片展示了一包重庆特色小吃——怪味胡豆")
    assert is_media_desc_text("[视频内容] 画面：桌面上有键盘和绿色包装袋")
    assert not is_media_desc_text("帮我介绍一下智聊的价格")
    assert not is_media_desc_text("你识唔识讲粤语噶？")
    assert not is_media_desc_text("")


def test_persona_kb_suppression():
    """陪聊人设（chat_binding）默认抑制业务 KB；kb_access: true 恢复；顾问会话不受影响。"""
    lin_xiaoyu = {"id": "lin_xiaoyu", "name": "林小雨", "role": "大学生"}
    assert persona_kb_suppressed(lin_xiaoyu, "chat_binding")
    assert persona_kb_suppressed(lin_xiaoyu, "account_profile")
    # 人设显式声明需要业务知识 → 放行
    support = {"id": "cs", "name": "客服小优", "kb_access": True}
    assert not persona_kb_suppressed(support, "chat_binding")
    # 未显式绑定（域/默认人设，如网页顾问顾嘉）→ 不抑制
    assert not persona_kb_suppressed(lin_xiaoyu, "domain")
    assert not persona_kb_suppressed(lin_xiaoyu, "default")
    assert not persona_kb_suppressed(None, "")


def test_content_tokens_filters_stopwords():
    toks = content_tokens("怎么付款呢")
    assert "怎么" not in toks
    assert any("付款" in t for t in toks)
    # 全虚词查询 → 空
    assert content_tokens("怎么办呢你哋") == [] or all(
        t not in ("怎么", "你哋") for t in content_tokens("怎么办呢你哋"))


def test_strong_english_token():
    """单个 ≥4 字英文强词（品牌/产品名）单独即可放行。"""
    entry = {"title": "EDIFIER 耳机推荐", "triggers": "EDIFIER 蓝牙耳机",
             "scenario": "", "category": "产品"}
    ok, why = lexical_overlap_ok("EDIFIER 好用吗", entry)
    assert ok and why.startswith("strong:")
