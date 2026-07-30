"""Phase 11 海外告警渠道（Telegram/WhatsApp/Messenger）+ webhook 覆盖层单测。"""

from __future__ import annotations

import json

from src.inbox.webhook_notifier import (
    WebhookNotifier,
    _build_chat_body,
    _plainify,
    _resolve_chat_endpoint,
)


def test_plainify_strips_markdown_and_relative_links():
    text = "**平台**: telegram\n[👥 前往账号管理](/workspace/unified-inbox)"
    out = _plainify(text)
    assert "**" not in out
    assert "前往账号管理" in out
    # 相对链接只保留文字
    assert "/workspace/unified-inbox" not in out


def test_plainify_keeps_absolute_links():
    out = _plainify("[文档](https://example.com/d)")
    assert "https://example.com/d" in out


def test_feishu_formatter_plainifies_markdown():
    """飞书 text 渠道不渲染 Markdown → formatter 必须先 _plainify（2026-07-31 事故：
    此前直接塞原始 md，飞书里显示字面 **粗体** + 相对链接死链）。飞书是用户首选的
    「贴 URL 即用」渠道，这条可读性回归影响面最大。"""
    import json as _json

    from src.inbox.webhook_notifier import _fmt_feishu
    raw = _fmt_feishu("🚨 **待审积压**",
                      "**账号**: a1\n[📊 查看运营总览](/admin/ops)", {})
    body = _json.loads(raw.decode("utf-8"))
    txt = body["content"]["text"]
    assert body["msg_type"] == "text"
    assert "**" not in txt, f"飞书残留 markdown 粗体符号: {txt!r}"
    assert "](/" not in txt, f"飞书残留相对链接死链: {txt!r}"
    # 内容文字保留（去符号不丢信息）
    assert "待审积压" in txt and "账号" in txt and "查看运营总览" in txt


def test_feishu_absolute_link_survives():
    """带 base_url 语义的绝对链接不该被吞（此处直接给 http 链接验证保留）。"""
    import json as _json

    from src.inbox.webhook_notifier import _fmt_feishu
    body = _json.loads(_fmt_feishu("t", "详情 [看这里](https://x.cc/ops)", {}).decode("utf-8"))
    assert "https://x.cc/ops" in body["content"]["text"]


def test_resolve_endpoint_telegram_from_token():
    url = _resolve_chat_endpoint("telegram", "", "123:ABC")
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"


def test_resolve_endpoint_messenger_from_token():
    url = _resolve_chat_endpoint("messenger", "", "PAGETOKEN")
    assert url.startswith("https://graph.facebook.com/")
    assert "access_token=PAGETOKEN" in url


def test_resolve_endpoint_whatsapp_needs_url():
    # whatsapp 无法仅凭 token 推断（需要 phone-id）
    assert _resolve_chat_endpoint("whatsapp", "", "TOKEN") == ""
    assert _resolve_chat_endpoint("whatsapp", "https://x/y", "TOKEN") == "https://x/y"


def test_resolve_endpoint_explicit_url_wins():
    assert _resolve_chat_endpoint("telegram", "https://custom/h", "tok") == "https://custom/h"


def test_build_chat_body_telegram():
    body, headers = _build_chat_body("telegram", "hi", "-100123", "")
    d = json.loads(body)
    assert d["chat_id"] == "-100123"
    assert d["text"] == "hi"
    assert "Authorization" not in headers


def test_build_chat_body_whatsapp_has_bearer():
    body, headers = _build_chat_body("whatsapp", "hi", "8613800000000", "TK")
    d = json.loads(body)
    assert d["messaging_product"] == "whatsapp"
    assert d["to"] == "8613800000000"
    assert d["text"]["body"] == "hi"
    assert headers["Authorization"] == "Bearer TK"


def test_build_chat_body_messenger():
    body, _ = _build_chat_body("messenger", "hi", "PSID1", "")
    d = json.loads(body)
    assert d["recipient"]["id"] == "PSID1"
    assert d["message"]["text"] == "hi"


def test_notifier_reload_rebuilds_matchers():
    n = WebhookNotifier(config=[])
    assert len(n._matchers) == 0
    n.reload([{
        "name": "tg", "format": "telegram", "token": "t", "target": "c",
        "events": ["autoreply_alert"],
    }])
    assert len(n._matchers) == 1
    assert n._matchers[0]["fmt"] == "telegram"
    assert n._matchers[0]["target"] == "c"
    # 禁用项不进匹配器
    n.reload([{"name": "x", "format": "telegram", "enabled": False,
               "events": ["all"]}])
    assert len(n._matchers) == 0


async def test_send_test_missing_target_returns_error():
    n = WebhookNotifier(config=[])
    res = await n.send_test({
        "name": "tg", "format": "telegram", "token": "tok", "target": "",
        "events": ["autoreply_alert"],
    })
    assert res["ok"] is False  # 缺 target → 计为错误


def test_merge_preserve_secrets_keeps_real_token_on_masked_resave():
    """运营接通告警的安全关键点：面板保存回传脱敏/空 token 时,不覆盖旧真实密钥。

    坏掉的后果：真 token 被 'abc***' 字面量覆盖 → 所有告警投递用错密钥静默失败
    （保存成功、告警发不出）。这是「接通告警出口」这条链上唯一有真逻辑的薄弱点,
    抽成纯函数在此钉住(此前内联在路由里不可测)。
    """
    from src.integrations.notify_webhooks_store import merge_preserve_secrets
    old = {"tg": {"name": "tg", "token": "REAL-123", "secret": "S-REAL"}}
    # 脱敏 token 回传 + 空 secret → 都保留旧真值
    out = merge_preserve_secrets(
        [{"name": "tg", "token": "REA***", "secret": ""}], old)
    assert out[0]["token"] == "REAL-123"
    assert out[0]["secret"] == "S-REAL"
    # 真新值 → 正常覆盖（改密钥要能生效）
    out2 = merge_preserve_secrets(
        [{"name": "tg", "token": "NEW-456", "secret": ""}], old)
    assert out2[0]["token"] == "NEW-456"
    assert out2[0]["secret"] == "S-REAL"   # secret 仍空 → 保留
    # 无同名旧条目 + 脱敏值 → 清空(绝不把 *** 当密钥存)
    out3 = merge_preserve_secrets([{"name": "new", "token": "abc***", "secret": ""}], {})
    assert out3[0]["token"] == ""


def test_store_sanitize_and_effective(tmp_path):
    from src.integrations import notify_webhooks_store as ws
    ws.set_store_path(tmp_path / "wh.json")
    try:
        # 覆盖层不存在 → 沿用 config.yaml
        base = {"notify": {"webhooks": [{"name": "y", "format": "json",
                                         "events": ["all"]}]}}
        assert ws.effective_webhooks(base)[0]["name"] == "y"
        # 保存覆盖层后整段取代
        saved = ws.save_list([
            {"name": "tg", "format": "telegram", "token": "secret-token",
             "target": "-100", "events": ["autoreply_alert", "不存在的别名"]},
            {"name": "bad", "format": "不存在的格式", "events": []},
        ])
        assert saved[0]["format"] == "telegram"
        assert saved[0]["events"] == ["autoreply_alert"]  # 非法别名剔除
        assert saved[1]["format"] == "json"  # 非法 format 回落
        assert saved[1]["events"] == ["autoreply_alert"]  # 空 events 兜底
        eff = ws.effective_webhooks(base)
        assert len(eff) == 2 and eff[0]["name"] == "tg"
        # 脱敏：token 不明文外泄
        masked = ws.mask(eff)
        assert masked[0]["token"].endswith("***")
        assert masked[0]["token_set"] is True
    finally:
        ws.set_store_path(tmp_path / "wh.json")
