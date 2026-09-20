# -*- coding: utf-8 -*-
"""WP-4 rider ②：主动触达披露（首触即 AI）门禁（2026-08-17）。

四组不变量：
1. **helper 单一入口** `apply_disclosure_for`：provider 未注册恒 no-op（不碰
   store）；notice 开首条前置/二条起原样；store 语言提示打通拉丁语系（es 客户
   出 es 披露语，不是 en）；显式 lang_hint 优先；任何异常原样放行；
2. **模板场景中性**：9 语披露语不得含「回复」语境动词（回复/ตอบ/trả lời）——
   同一条文案要同时服务「回复」与「主动开场」两场景（rider ② 的文案决策）；
3. **接线静态钉**：deferred 单一投递收口（background_tasks `_universal_send`，
   翻译之后）与 proactive_topic 开场发送（语言闸后、照片/语音分支前 + 披露命中
   强制文本）都必须走 helper；三处消费（含 sender.py）不得再出现各自拼样板的
   旧模式；
4. **强制文本语义**：proactive 披露命中 → 照片/语音分支跳过（`_disc_forced_text`
   守住两个分支）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config" / "config.yaml"))
    from src.compliance import disclosure, runtime

    disclosure._reset_cache_for_tests()
    runtime._reset_for_tests()
    yield tmp_path / "config"
    disclosure._reset_cache_for_tests()
    runtime._reset_for_tests()


def _enable(text=""):
    from src.compliance.runtime import set_config_provider
    set_config_provider(lambda: {
        "compliance": {"disclosure": {"notice": True, "text": text}}})


# ── 1. helper 单一入口 ───────────────────────────────────────────────────────

def test_helper_noop_when_provider_unset(iso):
    from src.compliance.disclosure import apply_disclosure_for, marks_count

    class _Boom:  # provider 未注册时绝不该碰 store
        def __getattr__(self, name):
            raise AssertionError("store touched while notice off")

    out, applied = apply_disclosure_for("telegram:a:1", "原样", store=_Boom())
    assert (out, applied) == ("原样", False)
    assert marks_count() == 0


def test_helper_applies_once_per_conversation(iso):
    from src.compliance.disclosure import apply_disclosure_for
    _enable()
    out1, a1 = apply_disclosure_for("telegram:a:2", "你好呀")
    out2, a2 = apply_disclosure_for("telegram:a:2", "第二条")
    assert a1 is True and "你好呀" in out1 and out1 != "你好呀"
    assert (out2, a2) == ("第二条", False)


def test_helper_store_hint_unlocks_latin_language(iso):
    """拉丁语系嗅探必回 en——store 提示在场才出 es 披露语（rider 的语言修复点）。"""
    from src.compliance.disclosure import _NOTICE_TEXTS, apply_disclosure_for
    _enable()

    class _Store:
        pass

    def _fake_hint(store, conversation_id):
        return "es"

    import src.inbox.outbound_translate as ot
    orig = ot.peer_language_hint
    ot.peer_language_hint = _fake_hint
    try:
        out, applied = apply_disclosure_for(
            "telegram:a:3", "hola, como estas hoy", store=_Store())
    finally:
        ot.peer_language_hint = orig
    assert applied is True
    assert out.startswith(_NOTICE_TEXTS["es"]), f"未按 store 提示出 es 披露语: {out[:80]}"


def test_helper_explicit_hint_beats_store(iso):
    from src.compliance.disclosure import _NOTICE_TEXTS, apply_disclosure_for
    _enable()

    class _Boom:
        def __getattr__(self, name):
            raise AssertionError("explicit hint given -- store must not be hit")

    out, applied = apply_disclosure_for(
        "telegram:a:4", "sample text", lang_hint="ja", store=_Boom())
    assert applied is True and out.startswith(_NOTICE_TEXTS["ja"])


# ── 2. 模板场景中性（回复/开场双场景通吃） ──────────────────────────────────

def test_notice_templates_are_context_neutral():
    from src.compliance.disclosure import _NOTICE_TEXTS
    banned = ("协助回复", "ช่วยตอบ", "trả lời")   # 回复语境动词（rider ② 决策）
    for lang, s in _NOTICE_TEXTS.items():
        for b in banned:
            assert b not in s, f"{lang} 披露语回潮回复语境动词 {b!r}: {s}"


# ── 3/4. 接线静态钉 ─────────────────────────────────────────────────────────

def test_deferred_universal_send_wired_after_translate():
    src = (ENGINE / "src" / "bootstrap" / "background_tasks.py").read_text(
        encoding="utf-8")
    i_tx = src.index("_maybe_translate_outbound")
    i_disc = src.index("apply_disclosure_for")
    i_orch = src.index("orch.send(platform, account_id, chat_key, text)")
    assert i_tx < i_disc < i_orch, "披露必须在翻译之后、编排器投递之前"


def test_proactive_topic_wired_with_forced_text():
    # 实施92b 修陈旧锚点：read-aware 批次给两个媒体分支叠加了 _rn_forced_text
    # 守卫（源码已提交）但本静态钉没跟上——锚点更新为语义级（守卫条件 →
    # 媒体调用相邻），不再钉缩进字面量（上次就是被换行/缩进变化打红的）。
    src = (ENGINE / "src" / "companion" / "proactive_topic.py").read_text(
        encoding="utf-8")
    i_disc = src.index("apply_disclosure_for")
    i_photo = src.index("and _photo_plan and await _try_send_photo(")
    i_voice = src.index("and await _try_send_voice(plan, text)")
    assert i_disc < i_photo and i_disc < i_voice, "披露判定必须在照片/语音分支之前"
    # 披露命中强制文本：两个媒体分支都必须被 _disc_forced_text 守住
    # （read-aware 的 _rn_forced_text 同一守卫位并列，两者缺一不可）
    assert re.search(
        r"if \(\(not _disc_forced_text\) and \(not _rn_forced_text\)"
        r"\s*\n\s*and _photo_plan and await _try_send_photo\(", src), \
        "照片分支未被披露/已读强制文本双守卫"
    assert re.search(
        r"if \(\(not _disc_forced_text\) and \(not _rn_forced_text\)"
        r"\s*\n\s*and await _try_send_voice\(", src), \
        "语音分支未被披露/已读强制文本双守卫"
    # 语言提示与开场文案生成同源
    assert "lang_hint=_peer_language(plan)" in src


def test_all_wiring_sites_use_single_helper():
    """三处出站接线（A 线 sender / deferred / proactive）必须全走 helper——
    谁再手拼 runtime_config+apply_disclosure 样板就在这红。"""
    sites = {
        "sender": ENGINE / "src" / "client" / "sender.py",
        "deferred": ENGINE / "src" / "bootstrap" / "background_tasks.py",
        "proactive": ENGINE / "src" / "companion" / "proactive_topic.py",
    }
    for name, p in sites.items():
        src = p.read_text(encoding="utf-8")
        assert "apply_disclosure_for" in src, f"{name} 未走统一 helper"
        assert "apply_disclosure(" not in src.replace(
            "apply_disclosure_for(", ""), \
            f"{name} 出现绕过 helper 的裸 apply_disclosure 调用"
