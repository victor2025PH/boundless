# -*- coding: utf-8 -*-
"""#152 F2 门禁：「工作目标没执行」从黑箱变可见。

证据：skuio 截图 _1094（0902 23:34）——右栏目标卡「第3/3天」看着在推进，对话零推进；
82VFQ6 全天日志里目标模块**零行**——build_block_for_chat 每个早退路径只写内存
meta（_goal_inject_meta / API goal_applied），既不落日志、前端也不消费。

钉住：
- 服务端：注入判定每轮落 INFO 日志（no_goal/disabled 降 DEBUG 防刷屏）；
- 前端：smart-reply 响应的 goal_applied 被消费，目标英雄行亮「上轮未按目标推进：原因」；
- i18n：每个 reason 码 zh/en/zh_hant 三语齐备。
"""
import inspect
import logging
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

REASONS = ("hold", "inactive", "observe", "beat_rejected", "empty_block",
           "outcome_auto_settled", "inject_disabled", "disabled", "no_engine",
           "unknown")


def test_note_meta_logs_not_injected(caplog):
    from src.companion.goals import service
    src = inspect.getsource(service.build_block_for_chat)
    assert "[goal-inject] NOT injected reason=" in src
    assert "[goal-inject] injected conv=" in src
    # no_goal 必须是 DEBUG（无目标会话每轮都走到这里）
    assert 'reason == "no_goal"' in src and "logger.debug(\"[goal-inject] skip=" in src


def test_frontend_consumes_goal_applied():
    html = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8")
    assert "_noteGoalApplied(cid, d.goal_applied)" in html, "smart-reply 响应必须消费 goal_applied"
    assert "var _goalLastApplied={}" in html
    assert "hg-skip" in html and "inbox.goal.hero.last_skip" in html
    css = (_ROOT / "src" / "web" / "static" / "workspace" / "unified-inbox.css").read_text(
        encoding="utf-8")
    assert ".cp-hero-goal .hg-skip" in css


def test_reason_keys_trilingual():
    from src.web.i18n_packs import goals as g
    zh = g.ZH if hasattr(g, "ZH") else None
    en = g.EN if hasattr(g, "EN") else None
    if zh is None or en is None:
        # 包结构不暴露常量名时按源码扫
        src = (_ROOT / "src" / "web" / "i18n_packs" / "goals.py").read_text(encoding="utf-8")
        for r in REASONS:
            assert src.count(f'"inbox.goal.skip.{r}"') >= 2, f"zh/en 缺键 {r}"
    else:
        for r in REASONS:
            assert f"inbox.goal.skip.{r}" in zh and f"inbox.goal.skip.{r}" in en
    hant = (_ROOT / "src" / "web" / "i18n_packs" / "zh_hant_auto.py").read_text(encoding="utf-8")
    for r in REASONS:
        assert f"'inbox.goal.skip.{r}'" in hant, f"繁中缺键 {r}"
    assert "'inbox.goal.hero.last_skip'" in hant
