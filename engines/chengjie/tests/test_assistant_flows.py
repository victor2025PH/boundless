# -*- coding: utf-8 -*-
"""小智业务流程注册表门禁（实施58 P5，2026-08-23）。

核心不变量：① 流程可触达端点收敛在登录路由家族白名单（谁加别的调用面
先红）；② 登录类流程恒 L3 且对坐席角色关闭；③ 双语文案齐平；④ 触发匹配
宁窄勿宽（不相关话语零误触发——误触发登录流比不触发扰人得多）。
"""
from __future__ import annotations

from src.assistant import flows as fl


def test_registry_validates_clean():
    assert fl.validate_registry() == []


def test_registry_urls_pinned_to_login_family():
    # 白名单本身也被钉住：谁扩前缀必须来这里解释
    assert set(fl.ALLOWED_URL_PREFIXES) == {
        "/api/platforms/telegram/login/",
        "/api/platforms/line/login/",
        "/api/platforms/telegram/login",
        "/api/platforms/line/login",
    }


def test_detect_flagship_triggers():
    hits = [
        "我要登陆飞机",
        "登录飞机",
        "帮我 登录 Telegram",
        "登陆TELEGRAM 一个新号",
        "加个飞机号",
        "login telegram please",
        "登录电报",
    ]
    for q in hits:
        assert fl.detect_flow_intent(q) == "tg_login", q


def test_detect_line_and_precedence():
    assert fl.detect_flow_intent("帮我登录line") == "line_login"
    # 「加个line号」不得被泛化触发词误抢
    assert fl.detect_flow_intent("加个line号") == "line_login"


def test_detect_no_false_positives():
    misses = [
        "把回复调快一点",
        "为什么不自动回复了",
        "今天天气怎么样",
        "帮我看看知识库",
        "客户说要退款怎么办",
        "",
        "x" * 500,  # 超长直接放弃
    ]
    for q in misses:
        assert fl.detect_flow_intent(q) == "", q


def test_role_gate_agent_excluded():
    assert fl.flow_allowed_for_role("tg_login", "master") is True
    assert fl.flow_allowed_for_role("tg_login", "agent") is False
    assert fl.flow_allowed_for_role("nope", "master") is False
    ids = {f["id"] for f in fl.catalog("agent", "zh")}
    assert ids == set()
    ids2 = {f["id"] for f in fl.catalog("master", "zh")}
    assert "tg_login" in ids2 and "line_login" in ids2


def test_client_spec_lang_resolution():
    zh = fl.client_spec("tg_login", "zh")
    en = fl.client_spec("tg_login", "en")
    assert zh and en
    assert zh["texts"]["confirm"] != en["texts"]["confirm"]
    assert "{login_id}" in zh["status_url"]
    assert zh["cancel_url"].endswith("/cancel")
    assert fl.client_spec("nope") is None
    # 文案键齐全（前端运行器消费面）
    for k in ("say", "confirm", "qr_hint", "scanned", "pwd", "requery", "ok"):
        assert zh["texts"].get(k), k
