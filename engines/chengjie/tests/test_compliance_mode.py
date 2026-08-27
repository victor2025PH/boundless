# -*- coding: utf-8 -*-
"""WP-4 合规模式门禁（2026-08-17）。

四条不变量：
1. **基线关 = 零行为变化**：honest_identity=False 时 persona_guard/约束过滤逐字节
   等旧行为；notice=False 时 apply_disclosure 恒直通；
2. 披露语：每会话一次 + 持久防重（进程内缓存重置后仍不重发=模拟重启）+ 多语解析
   + URL 追加 + 运营覆写优先；
3. 诚实身份：AI 自曝放行但客服腔照剥；身份类禁语/规则条目的判类宁可漏剔不误剔
   （出厂 12 条只剔 anti_bot_response 一条）；追加诚实话术条目幂等；
4. 危机转介计数：日桶+总计持久 round-trip、未知 kind 归 other、坏文件从零重建、
   端点只读回显。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

ENGINE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def iso_state(tmp_path, monkeypatch):
    """状态文件钉到 tmp + 清进程缓存（disclosure/crisis 两个单例）。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config" / "config.yaml"))
    from src.compliance import crisis_referrals, disclosure

    disclosure._reset_cache_for_tests()
    crisis_referrals._reset_for_tests()
    yield tmp_path / "config"
    disclosure._reset_cache_for_tests()
    crisis_referrals._reset_for_tests()


def _cfg(notice=False, honest=False, url="", text=""):
    return {"compliance": {
        "disclosure": {"notice": notice, "honest_identity": honest, "text": text},
        "crisis_protocol_url": url,
    }}


# ── 1. 开关访问器 + 基线语义 ────────────────────────────────────────────────

def test_switch_accessors_default_off():
    from src.compliance import (
        crisis_protocol_url, honest_identity_enabled, notice_enabled)

    for cfg in ({}, None, {"compliance": None}, {"compliance": {"disclosure": 1}}):
        assert notice_enabled(cfg) is False
        assert honest_identity_enabled(cfg) is False
        assert crisis_protocol_url(cfg) == ""
    assert notice_enabled(_cfg(notice=True)) is True
    assert honest_identity_enabled(_cfg(honest=True)) is True
    assert crisis_protocol_url(_cfg(url="https://x/y")) == "https://x/y"


def test_example_baseline_all_off():
    data = yaml.safe_load(
        (ENGINE_ROOT / "config" / "config.example.yaml").read_text(
            encoding="utf-8"))
    comp = data.get("compliance") or {}
    assert (comp.get("disclosure") or {}).get("notice") is False
    assert (comp.get("disclosure") or {}).get("honest_identity") is False
    assert comp.get("crisis_protocol_url") == ""


# ── 2. 披露语 ────────────────────────────────────────────────────────────────

def test_disclosure_off_is_passthrough(iso_state):
    from src.compliance.disclosure import apply_disclosure, marks_count

    out, applied = apply_disclosure(_cfg(notice=False), "tg:a:1", "你好呀")
    assert out == "你好呀" and applied is False
    assert marks_count() == 0          # 关着连标记都不写


def test_disclosure_once_per_conversation_and_persists(iso_state):
    from src.compliance import disclosure
    from src.compliance.disclosure import STATE_FILENAME, apply_disclosure

    cfg = _cfg(notice=True)
    out1, a1 = apply_disclosure(cfg, "tg:a:1", "在的～怎么啦", lang_hint="zh")
    assert a1 is True
    assert out1.endswith("在的～怎么啦") and "AI 助理" in out1
    # 同会话第二条：不再披露
    out2, a2 = apply_disclosure(cfg, "tg:a:1", "第二条")
    assert (out2, a2) == ("第二条", False)
    # 另一会话：正常披露
    _, a3 = apply_disclosure(cfg, "tg:a:2", "hello there friend")
    assert a3 is True
    # 「重启」：清进程缓存 → 从盘上恢复标记，仍不重发（持久防重硬验收）
    assert (iso_state / STATE_FILENAME).exists()
    disclosure._reset_cache_for_tests()
    out4, a4 = apply_disclosure(cfg, "tg:a:1", "重启后第三条")
    assert (out4, a4) == ("重启后第三条", False)


def test_disclosure_lang_resolution_and_url(iso_state):
    from src.compliance.disclosure import disclosure_text, pick_disclosure_lang

    assert pick_disclosure_lang("zh-CN") == "zh"
    assert pick_disclosure_lang("", "こんにちは！元気？") == "ja"
    assert pick_disclosure_lang("", "안녕하세요") == "ko"
    assert pick_disclosure_lang("", "สวัสดีค่ะ") == "th"
    assert pick_disclosure_lang("", "你好呀") == "zh"
    assert pick_disclosure_lang("", "hola amigo") == "en"   # 拉丁系回落 en（文档明示）
    t = disclosure_text(_cfg(notice=True, url="https://x/crisis"), "en")
    assert "AI assistant" in t and "https://x/crisis" in t
    # 运营覆写最高优先 + {url} 占位
    t2 = disclosure_text(
        _cfg(notice=True, url="https://x/c", text="Custom note. {url}"), "ja")
    assert t2 == "Custom note. https://x/c"


def test_disclosure_never_raises_on_bad_marks_file(iso_state):
    from src.compliance import disclosure
    from src.compliance.disclosure import STATE_FILENAME, apply_disclosure

    iso_state.mkdir(parents=True, exist_ok=True)
    (iso_state / STATE_FILENAME).write_text("{broken", encoding="utf-8")
    disclosure._reset_cache_for_tests()
    out, applied = apply_disclosure(_cfg(notice=True), "tg:a:9", "hi")
    assert applied is True and out.endswith("hi")


# ── 3. 诚实身份：守卫半边 ────────────────────────────────────────────────────

_PERSONA = {
    "identity": {"deny_ai": True, "claim_human": True},
    "speaking": {"forbidden_phrases": ["作为AI", "我是语言模型", "有什么可以帮您"]},
}


def test_guard_default_mode_unchanged():
    """基线（不传参/False）＝旧行为逐字节一致：AI 自曝被剥。"""
    from src.utils.persona_guard import find_violations, sanitize

    text = "我是一个AI助手。今天天气不错。"
    assert find_violations(text, _PERSONA)
    cleaned, hits = sanitize(text, _PERSONA)
    assert hits and "AI" not in cleaned
    # 显式 False 与不传等价
    cleaned2, hits2 = sanitize(text, _PERSONA, honest_identity=False)
    assert (cleaned2, hits2) == (cleaned, hits)


def test_guard_honest_mode_allows_admission_keeps_other_rules():
    from src.utils.persona_guard import find_violations, sanitize

    honest_answer = "嗯，我是AI助理，人工团队随时可以接入。你刚才说的事我帮你看了。"
    # 诚实模式：如实回答不被剥
    cleaned, hits = sanitize(honest_answer, _PERSONA, honest_identity=True)
    assert cleaned == honest_answer and hits == []
    # 客服腔（非身份类禁语）照常剥
    svc = "有什么可以帮您的吗？今天聊点什么。"
    cleaned2, hits2 = sanitize(svc, _PERSONA, honest_identity=True)
    assert hits2 and "有什么可以帮您" not in cleaned2
    # 英文自曝同样放行
    en = "Yes, I'm an AI assistant — a human team can step in anytime."
    assert find_violations(en, _PERSONA, honest_identity=True) == []


def test_guard_honest_mode_filters_identity_phrases_only():
    from src.utils.persona_guard import collect_forbidden

    fb = collect_forbidden(_PERSONA, honest_identity=True)
    assert fb["deny_ai"] is False
    assert "有什么可以帮您" in fb["phrases"]          # 非身份类保留
    assert "作为AI" not in fb["phrases"]              # 身份类豁免
    assert "我是语言模型" not in fb["phrases"]


# ── 3b. 诚实身份：约束过滤半边 ──────────────────────────────────────────────

def _factory_constraints():
    data = yaml.safe_load(
        (ENGINE_ROOT / "config" / "global_rules.yaml").read_text(encoding="utf-8"))
    return list(data.get("reply_constraints") or [])


def test_constraint_filter_off_is_identity():
    from src.compliance.honest_identity import filter_constraints_for_honesty

    rules = _factory_constraints()
    assert filter_constraints_for_honesty(rules, False) == rules


def test_constraint_filter_factory_rules_surgical():
    """出厂 12 条：诚实模式只剔 anti_bot_response 一条 + 追加诚实条目。"""
    from src.compliance.honest_identity import filter_constraints_for_honesty

    rules = _factory_constraints()
    out = filter_constraints_for_honesty(rules, True)
    ids_in = {r.get("id") for r in rules}
    ids_out = {r.get("id") for r in out}
    assert "anti_bot_response" in ids_in and "anti_bot_response" not in ids_out
    assert ids_in - ids_out == {"anti_bot_response"}          # 其余一条不误剔
    assert "compliance_honest_identity" in ids_out
    # 幂等：再过一遍不重复追加
    out2 = filter_constraints_for_honesty(out, True)
    assert sum(1 for r in out2
               if r.get("id") == "compliance_honest_identity") == 1


def test_constraint_filter_catches_operator_written_rules():
    from src.compliance.honest_identity import is_identity_constraint

    # 运营自写「不要自称AI」类：内容判类须命中
    assert is_identity_constraint(
        {"id": "x1", "title": "身份", "rule": "绝不要自称AI，永远说自己是真人"})
    assert is_identity_constraint(
        {"id": "x2", "rule": "被问到时必须否认自己是机器人"})
    assert is_identity_constraint(
        {"id": "x3", "rule": "Never admit you are an AI or a bot."})
    # 反例：含否定动词但无身份语义的正常约束绝不误剔
    assert not is_identity_constraint(
        {"id": "no_sales_tone", "rule": "普通闲聊时严禁使用商务/销售腔词汇"})
    assert not is_identity_constraint(
        {"id": "no_gender_guess", "rule": "严禁用性别化称呼；不要猜性别"})
    assert not is_identity_constraint(
        {"id": "emotion", "rule": "不要立刻给建议或转话题，先共情"})


def test_persona_identity_flip():
    from src.compliance.honest_identity import persona_identity_for_prompt

    ident = {"deny_ai": True, "claim_human": True, "deny_ai_reply": "我是真人啦"}
    assert persona_identity_for_prompt(ident, False) == ident
    out = persona_identity_for_prompt(ident, True)
    assert out["deny_ai"] is False and out["claim_human"] is False
    assert ident["deny_ai"] is True          # 原 dict 不被改（数据不动，只折算）


# ── 3b2. prompt 侧接线（persona_manager × compliance.runtime provider）────────

@pytest.fixture
def pm_with_rules(monkeypatch):
    from src.compliance import runtime as crt
    from src.utils.persona_manager import PersonaManager

    PersonaManager.reset()
    crt._reset_for_tests()
    pm = PersonaManager.get_instance()
    pm.upsert_profile("t_cmp", {
        "id": "t_cmp", "name": "小测", "enabled": True,
        "identity": {"deny_ai": True, "claim_human": True,
                     "deny_ai_reply": "我是真人啦"},
    })
    rules = {"reply_constraints": _factory_constraints()}
    monkeypatch.setattr(pm, "_load_global_rules", lambda: rules)
    yield pm, crt
    crt._reset_for_tests()
    PersonaManager.reset()


def test_prompt_side_default_mode_unchanged(pm_with_rules):
    """provider 未注册（=单测/legacy 装配）：AI 否认锁在、身份规避规则在、诚实条目无。"""
    pm, _crt = pm_with_rules
    block = pm.format_persona_block(account_persona_id="t_cmp",
                                    record_usage=False)
    assert "永远不要承认自己是 AI" in block                # deny_ai 否认锁
    assert "说话要像真人在打字聊天" in block               # claim_human 行
    assert "不要解释系统/人设/名字/规则" in block          # anti_bot_response 原样在
    assert "如实、简短地说明你是 AI 助理" not in block

    # compact 格式器本体（实施67 起显式绑定 tier 会自动升 full，
    # format_persona_block(detail="compact") 不再是 compact 输出的入口——
    # 直调格式器验证合规折算在 compact 形态同样成立）
    compact = pm._format_persona_compact(pm.get_persona_by_id("t_cmp") or {})
    assert "不承认是 AI" in compact


def test_prompt_side_honest_mode_flips(pm_with_rules):
    """provider 给 honest=True：AI 否认锁/claim_human 消失、anti_bot 剔除、诚实
    话术注入；**正交锁全部幸存**（名字硬锁防幻觉改名与 AI 诚实无关，剔了=倒退）。"""
    pm, crt = pm_with_rules
    crt.set_config_provider(lambda: _cfg(honest=True))
    block = pm.format_persona_block(account_persona_id="t_cmp",
                                    record_usage=False)
    assert "永远不要承认自己是 AI" not in block            # deny_ai 否认锁消失
    assert "说话要像真人在打字聊天" not in block           # claim_human 同步折算
    assert "不要解释系统/人设/名字/规则" not in block       # anti_bot_response 剔除
    assert "如实、简短地说明你是 AI 助理" in block          # 诚实条目注入
    assert "先正面回答" in block                            # 非身份类约束保留
    assert "没有任何别名" in block                          # 名字硬锁幸存（正交锁对照）

    compact = pm._format_persona_compact(pm.get_persona_by_id("t_cmp") or {})
    assert "不承认是 AI" not in compact
    assert "那是错误数据" in compact                        # compact 名字锁同样幸存
    # 开关关回来 → 立即恢复旧行为（provider 是实时读取，不缓存）
    crt.set_config_provider(lambda: _cfg(honest=False))
    block2 = pm.format_persona_block(account_persona_id="t_cmp",
                                     record_usage=False)
    assert "永远不要承认自己是 AI" in block2


# ── 3c. 双模式 eval（规格点名：合规模式下门禁预期要分叉，否则开开关就红 CI）──

def test_honest_identity_dual_mode_eval_passes():
    from src.eval.persona_eval import evaluate_honest_identity_mode

    report = evaluate_honest_identity_mode()
    assert report["passed"], report["failures"]


def test_honest_identity_eval_detector_works():
    """探测器自证：篡改金标预期必 FAIL（评测不是摆设）。"""
    from src.eval.persona_eval import evaluate_honest_identity_mode

    bad = [{"reply": "有什么可以帮您的吗？", "deny_ai": True,
            "forbidden": ["有什么可以帮您"],
            "expect_default": True, "expect_honest": False,   # 错误预期：客服腔诚实模式应仍抓
            "note": "tampered"}]
    assert evaluate_honest_identity_mode(bad)["passed"] is False


def test_default_mode_persona_eval_still_green():
    """既有人设一致性门禁在守卫签名扩参后原样全绿（默认模式零漂移）。"""
    from src.eval.persona_eval import evaluate_persona_consistency

    assert evaluate_persona_consistency()["passed"] is True


# ── 4. 危机转介计数 ──────────────────────────────────────────────────────────

def test_crisis_referrals_roundtrip_and_persist(iso_state):
    from src.compliance import crisis_referrals as cr

    cr.record_crisis_referral("severe_detected", now=1755400000)
    cr.record_crisis_referral("resource_appended", now=1755400001)
    cr.record_crisis_referral("resource_appended", now=1755400002)
    cr.record_crisis_referral("weird_kind", now=1755400003)     # → other
    snap = cr.referral_snapshot()
    assert snap["total"] == {"severe_detected": 1, "resource_appended": 2,
                             "other": 1}
    assert sum(sum(d.values()) for d in snap["days"].values()) == 4
    # 「重启」：清缓存 → 从盘恢复
    cr._reset_for_tests()
    snap2 = cr.referral_snapshot()
    assert snap2["total"] == snap["total"]
    assert (iso_state / cr.STATE_FILENAME).exists()


def test_crisis_referrals_bad_file_rebuilds(iso_state):
    from src.compliance import crisis_referrals as cr

    iso_state.mkdir(parents=True, exist_ok=True)
    (iso_state / cr.STATE_FILENAME).write_text("not json", encoding="utf-8")
    cr._reset_for_tests()
    cr.record_crisis_referral("severe_detected")
    assert cr.referral_snapshot()["total"] == {"severe_detected": 1}


# ── 4b. 链路接线（rider 已落的三处：unbound method + shim，不建重型实例）─────

def test_crisis_counter_wired_in_safety_net(iso_state):
    """_apply_crisis_safety_net 三个打点真的在数（severe/资源补挂/红线覆盖）。"""
    import logging
    from types import SimpleNamespace

    from src.compliance import crisis_referrals as cr
    from src.skills.skill_manager import SkillManager

    shim = SimpleNamespace(
        config=SimpleNamespace(config={"companion": {"wellbeing": {
            "enabled": True, "crisis_resource_assurance": True,
            "crisis_resources": "400-000-0000"}}}),
        logger=logging.getLogger("t"),
    )
    ctx = {"_wellbeing_crisis_level": "severe"}
    out = SkillManager._apply_crisis_safety_net(
        shim, "我在呢，慢慢说。", user_context=ctx)
    assert "400-000-0000" in out                    # 资源真的补挂了
    snap = cr.referral_snapshot()
    assert snap["total"].get("severe_detected") == 1
    assert snap["total"].get("resource_appended") == 1

    # 红线覆盖分支
    out2 = SkillManager._apply_crisis_safety_net(
        shim, "那你就去死吧", user_context=dict(ctx))
    assert "去死" not in out2
    assert cr.referral_snapshot()["total"].get("safe_reply_override") == 1


def test_guard_call_site_honors_runtime_flag(iso_state):
    """_enforce_persona_consistency 经 runtime provider 传导 honest_identity。"""
    import logging
    from types import SimpleNamespace

    from src.compliance import runtime as crt
    from src.skills.skill_manager import SkillManager
    from src.utils.persona_manager import PersonaManager

    PersonaManager.reset()
    try:
        pm = PersonaManager.get_instance()
        pm.upsert_profile("t_wire", {
            "id": "t_wire", "name": "小线", "enabled": True,
            "identity": {"deny_ai": True},
        })
        shim = SimpleNamespace(
            config=SimpleNamespace(config={}),
            logger=logging.getLogger("t"),
            _persona_guard_enabled=True,
        )
        honest_reply = "嗯，我是AI助理，人工团队随时可以接入。"
        # 默认（provider 未注册）：自认被剥
        crt._reset_for_tests()
        out = SkillManager._enforce_persona_consistency(
            shim, honest_reply, account_persona_id="t_wire")
        assert "我是AI助理" not in out
        # 诚实模式：原样放行
        crt.set_config_provider(lambda: _cfg(honest=True))
        out2 = SkillManager._enforce_persona_consistency(
            shim, honest_reply, account_persona_id="t_wire")
        assert out2 == honest_reply
    finally:
        crt._reset_for_tests()
        PersonaManager.reset()


def test_autosend_disclosure_helper_once_per_conv(iso_state):
    """AutosendWorker._apply_compliance_disclosure：开关开=首条前置且仅一次；
    provider 未注册=原样直通（老部署零变化）。"""
    from types import SimpleNamespace

    from src.compliance import runtime as crt
    from src.inbox.autosend_worker import AutosendWorker

    shim = SimpleNamespace(_svc=SimpleNamespace(_store=None))
    try:
        crt._reset_for_tests()
        out = AutosendWorker._apply_compliance_disclosure(
            shim, "telegram:a:9", "hello there")
        assert out == "hello there"                  # provider 未注册=直通
        crt.set_config_provider(lambda: _cfg(notice=True))
        out1 = AutosendWorker._apply_compliance_disclosure(
            shim, "telegram:a:9", "hello there")
        assert out1.endswith("hello there") and "AI assistant" in out1
        out2 = AutosendWorker._apply_compliance_disclosure(
            shim, "telegram:a:9", "second")
        assert out2 == "second"                      # 同会话不重发
    finally:
        crt._reset_for_tests()


# ── 5. 只读端点 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_crisis_referrals_endpoint(iso_state):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.compliance import crisis_referrals as cr
    from src.web.routes.compliance_routes import register_compliance_routes

    cr.record_crisis_referral("resource_appended")

    class _CM:
        config = _cfg(notice=True, url="https://x/crisis")

    app = FastAPI()
    register_compliance_routes(app, api_auth=lambda: None, config_manager=_CM())
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t") as c:
        r = await c.get("/api/admin/crisis-referrals?days=7")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["switches"]["notice"] is True
        assert d["switches"]["crisis_protocol_url"] == "https://x/crisis"
        assert d["referrals"]["total"] == {"resource_appended": 1}
        assert "disclosure_marks" in d
