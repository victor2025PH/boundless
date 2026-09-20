# -*- coding: utf-8 -*-
"""球 ↔ 教学/代办兄弟组件的 DOM 协奏词汇表门禁（实施59 P3，2026-08-23）。

背景：teach 线门禁（test_assistant_teach_wireup::test_zero_symbol_dependency_
on_ball_module）钉死教学/代办模块对球「零 JS 符号依赖」——两条线各自快速
重构，符号级耦合会把它们焊死。因此协奏走**对称的 DOM 协作**：
  - 他们操作球的公开 DOM（点 .asb-x 收面板、读 .asb-orb 渐变当流星色）；
  - 球观察他们挂在 body 的公开标记点亮状态灯：
      .xzt-banner                        = 教学模式进行中
      .xza-card + [data-xza="stop"]      = 代办任务执行中
      .xza-step 里 .ic 的 ✓/✗/▷          = 步骤完成计数（进度弧分子）

本门禁把这份共享词汇**双向钉住**：任何一侧改类名/图标，先红在这里＝显式
重新协商，而不是让观察者静默失明（软失败设计意味着失明不会报错——正因
如此才需要静态门禁兜底）。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BALL = ROOT / "shared" / "assistant" / "assistant-ball.js"
TEACH = ROOT / "shared" / "assistant" / "assistant-teach.js"
AGENT = ROOT / "shared" / "assistant" / "assistant-agent.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_ask_event_vocabulary_pinned_both_sides():
    """教学模式「详细讲讲」→ 球开面板投问。球开合走 pointerup 不是 click，
    协作词汇是 CustomEvent asb-ask + AssistantBall.ask（同源）。"""
    teach = _read(TEACH)
    ball = _read(BALL)
    assert "asb-ask" in teach, (
        "assistant-teach.js 不再派发 asb-ask——旧球缓存无 ask() 时投问会哑火"
    )
    assert "asb-ask" in ball, (
        "assistant-ball.js 不再监听 asb-ask——教学投问事件会进虚空"
    )
    assert "ask: askFromOutside" in ball or "ask: askFromOutside," in ball, (
        "AssistantBall.ask 公共入口缺失——teach 线的主投问路断了"
    )


def test_teach_banner_vocabulary_pinned_both_sides():
    assert "xzt-banner" in _read(TEACH), (
        "assistant-teach.js 不再产出 .xzt-banner——球的教学态观察器将静默失明；"
        "改类名请与 orb 线协商并同步 assistant-ball.js::sibScan"
    )
    assert "'.xzt-banner'" in _read(BALL), (
        "assistant-ball.js 不再观察 .xzt-banner——教学态状态灯断线"
    )


def test_agent_card_vocabulary_pinned_both_sides():
    agent = _read(AGENT)
    ball = _read(BALL)
    for token, why in (
        ('class="stop" data-xza="stop"', "执行中停止按钮（球的 agent 态判据）"),
        ("xza-card", "任务卡容器"),
        ("xza-step", "步骤行"),
    ):
        assert token in agent, (
            f"assistant-agent.js 缺 {token!r}（{why}）——球的代办态观察器将失明；"
            "改 DOM 请与 orb 线协商"
        )
    assert '\'[data-xza="stop"]\'' in ball, (
        "assistant-ball.js 不再按 [data-xza=stop] 判执行中——代办态断线"
    )
    assert "'.xza-step'" in ball and "'.ic'" in ball, (
        "assistant-ball.js 步骤进度选择器（.xza-step/.ic）缺席"
    )


def test_step_icon_glyphs_pinned_both_sides():
    """进度弧分子=数「已终结」图标 ✓✗▷；agent 侧 stIcon 表与球侧计数字符
    必须同字。图标换字（如 ✓→✔）会让进度弧悄悄停摆。"""
    agent = _read(AGENT)
    ball = _read(BALL)
    for ch in ("✓", "✗", "▷"):
        assert ch in agent, f"assistant-agent.js stIcon 表缺 {ch!r}"
        assert ch in ball, f"assistant-ball.js 步骤计数缺 {ch!r}"


def test_sibling_markers_are_body_level():
    """球的观察器只挂 body childList（零热区噪声的前提）——两个标记必须
    直接 appendChild 到 document.body。搬进容器请同步 orbWatchSiblings。"""
    assert "document.body.appendChild(banner)" in _read(TEACH), (
        ".xzt-banner 不再直挂 body——球的 childList 观察将看不见它"
    )
    agent = _read(AGENT)
    assert "document.body.appendChild(c)" in agent, (
        ".xza-card 不再直挂 body——球的 childList 观察将看不见它"
    )
