# -*- coding: utf-8 -*-
"""微信客服平台级强制合规门禁（实施97 线 A）。

大陆法域下 AI 身份可识别是红线：这些断言钉住「运营开关全关，微信客服上披露与诚实身份
仍然恒开；其余平台行为逐字节不变」。
"""
from __future__ import annotations

import pytest

from src import compliance as C
from src.compliance import runtime as R


@pytest.fixture(autouse=True)
def _reset():
    R._reset_for_tests()
    from src.compliance import disclosure as D
    D._reset_cache_for_tests()
    yield
    R._reset_for_tests()
    D._reset_cache_for_tests()


def test_forced_platform_flags_and_key_parsing():
    assert C.platform_forces_compliance("wechat_kf") and C.platform_forces_compliance("WeChat_KF ")
    assert not C.platform_forces_compliance("telegram") and not C.platform_forces_compliance("")
    assert C.platform_of_conversation_key("wechat_kf:wkKF:wxkf:user:u1") == "wechat_kf"
    assert C.platform_of_conversation_key("plain") == "" and C.platform_of_conversation_key(None) == ""


def test_switches_forced_only_on_forced_platform():
    assert C.notice_enabled({}) is False and C.honest_identity_enabled({}) is False
    assert C.notice_enabled({}, platform="wechat_kf") is True
    assert C.honest_identity_enabled({}, platform="wechat_kf") is True
    assert C.notice_enabled({}, platform="telegram") is False
    # 显式配置在非强制平台照常生效
    cfg = {"compliance": {"disclosure": {"notice": True, "honest_identity": True}}}
    assert C.notice_enabled(cfg, platform="telegram") and C.honest_identity_enabled(cfg)


def test_platform_scope_drives_runtime_flags_and_resets():
    assert R.current_platform() == "" and R.honest_identity_active() is False
    with R.platform_scope("wechat_kf"):
        assert R.current_platform() == "wechat_kf"
        assert R.honest_identity_active() is True and R.notice_active() is True
        with R.platform_scope("telegram"):
            assert R.honest_identity_active() is False
        assert R.honest_identity_active() is True
    assert R.current_platform() == "" and R.honest_identity_active() is False
    with R.platform_scope(""):
        assert R.current_platform() == ""


async def test_scope_propagates_into_tasks_and_threads():
    import asyncio

    async def _probe():
        return R.honest_identity_active()

    with R.platform_scope("wechat_kf"):
        assert await asyncio.create_task(_probe()) is True
        assert await asyncio.to_thread(R.honest_identity_active) is True
    assert await asyncio.create_task(_probe()) is False


def test_apply_disclosure_forced_by_conversation_key_prefix():
    from src.compliance.disclosure import apply_disclosure, apply_disclosure_for
    out, applied = apply_disclosure({}, "wechat_kf:wkKF:wxkf:user:u1", "您好，请问有什么可以帮您")
    assert applied and out.startswith("您好～本对话由 AI 助理协助") and out.endswith("请问有什么可以帮您")
    # 同会话第二条不再重复披露
    out2, applied2 = apply_disclosure({}, "wechat_kf:wkKF:wxkf:user:u1", "第二句")
    assert not applied2 and out2 == "第二句"
    # 非强制平台 + 开关关 → 原样
    out3, applied3 = apply_disclosure({}, "telegram:acct:123", "hello")
    assert not applied3 and out3 == "hello"
    # 一站式入口同口径（provider 未注册 → 空配置，但前缀强制仍生效）
    out4, applied4 = apply_disclosure_for("wechat_kf:wkKF:wxkf:user:u2", "hello there")
    assert applied4 and "AI assistant" in out4


async def test_generate_persona_reply_enters_platform_scope(monkeypatch):
    from src.inbox import persona_reply as PR
    seen = {}

    async def _impl(**kw):
        seen["platform"] = R.current_platform()
        seen["honest"] = R.honest_identity_active()
        seen["kw"] = kw
        return {"reply": "ok"}

    monkeypatch.setattr(PR, "_generate_persona_reply_impl", _impl)
    res = await PR.generate_persona_reply(app=None, platform="wechat_kf", chat_key="wxkf:user:u",
                                          last_inbound="hi", history=[], persona_id="p1",
                                          inbound_msg_id="m1")
    assert res == {"reply": "ok"}
    assert seen["platform"] == "wechat_kf" and seen["honest"] is True
    assert seen["kw"]["persona_id"] == "p1" and seen["kw"]["inbound_msg_id"] == "m1"
    assert R.current_platform() == ""
    await PR.generate_persona_reply(app=None, platform="telegram", chat_key="1",
                                    last_inbound="hi", history=[])
    assert seen["honest"] is False


def test_persona_manager_reads_forced_identity(monkeypatch):
    """人设 deny_ai=true 在强制平台按 False 处理（prompt 半边）。"""
    from src.compliance.honest_identity import persona_identity_for_prompt
    identity = {"deny_ai": True, "claim_human": True}
    with R.platform_scope("wechat_kf"):
        ident = persona_identity_for_prompt(identity, R.honest_identity_active())
    assert ident.get("deny_ai") is False and ident.get("claim_human") is False
    ident2 = persona_identity_for_prompt(identity, R.honest_identity_active())
    assert ident2.get("deny_ai") is True
