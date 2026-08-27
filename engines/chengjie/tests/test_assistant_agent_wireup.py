# -*- coding: utf-8 -*-
"""「替我做」智能体前端接线门禁（实施58 P2，2026-08-23）。

与 test_assistant_teach_wireup 同族四不变量：版本戳双模板同步 /
零内联 handler / 内置词典 zh-en 齐平 / 对球模块零符号依赖（形象线解耦）。
另钉：执行只许走 /api/assistant/act 家族（禁止出现第二条写通道 URL）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_JS = ROOT / "shared" / "assistant" / "assistant-agent.js"
TEMPLATES = [
    ROOT / "src" / "web" / "templates" / "base.html",
    ROOT / "src" / "web" / "templates" / "workspace_base.html",
]


def _src() -> str:
    return AGENT_JS.read_text(encoding="utf-8")


def test_agent_js_exists_and_nontrivial():
    assert AGENT_JS.is_file()
    assert AGENT_JS.stat().st_size > 8000


def test_version_stamp_synced_across_loaders():
    src = _src()
    m = re.search(r"var VER = '([0-9a-z]+)'", src)
    assert m, "assistant-agent.js 缺 VER 声明"
    ver = m.group(1)
    for tpl in TEMPLATES:
        text = tpl.read_text(encoding="utf-8")
        stamps = re.findall(r"assistant-agent\.js\?v=([0-9a-z]+)", text)
        assert stamps, f"{tpl.name} 未接线 assistant-agent.js"
        assert all(s == ver for s in stamps), (
            f"{tpl.name} ?v={stamps} != VER={ver}——改组件必须同批 bump 加载器"
        )


def test_no_inline_event_handlers_in_generated_html():
    # 负向断言防误伤：textContent = '…' 里的 "ontent" 不是 on* 属性
    # （与 inline-color 门禁同款「缺词边界」教训，2026-08-23 双双修正）
    bad = re.findall(r"(?<![a-zA-Z])on[a-z]{3,20}\s*=\s*[\"']", _src())
    assert not bad, f"内联 handler 痕迹：{bad[:3]}"


def test_loaders_pass_page_to_bootstrap():
    """P1 页级 chips 前提：loader 的 bootstrap 探针必须带 ?page=——
    掉了这个参数，页级 chips 静默退化成全局 chips（无报错难发现）。"""
    for tpl in TEMPLATES:
        text = tpl.read_text(encoding="utf-8")
        assert "/api/assistant/bootstrap?page='+encodeURIComponent" in text, (
            f"{tpl.name} 的 bootstrap 探针未带 page 参数"
        )


def test_clarify_ask_ui_wired():
    """clarify 追问（P1）：ask 渲染块 + 一轮防拉扯（clarified 只传一次）。"""
    src = _src()
    assert "xza-ask" in src, "缺 ask 渲染块"
    assert "ask-send" in src, "缺 ask 提交按钮"
    assert "j.ask && !clarified" in src, "缺一轮防拉扯守卫"


def test_history_center_wired():
    """任务历史（P1）：入口 + 端点 + 撤销转投既有 undo 路由。"""
    src = _src()
    assert "/api/assistant/act/history" in src
    assert "hist-undo" in src
    assert "openHistory" in src


def test_no_comment_terminator_footgun():
    """同 teach 门禁：块注释里 `-*/` = 整文件 SyntaxError 静默哑火。"""
    assert "-*/" not in _src()


def test_builtin_dict_zh_en_parity():
    src = _src()
    used = set(re.findall(r"\bt\('([a-z_0-9]+)'\)", src))
    assert used
    for key in sorted(used):
        defs = re.findall(rf"^\s+{key}:\s", src, flags=re.M)
        assert len(defs) >= 2, f"词典键 {key!r} 缺 zh/en 之一（{len(defs)}）"


def test_zero_symbol_dependency_on_ball_module():
    src = _src()
    assert "AssistantBall." not in src.replace("window.AssistantBall", ""), (
        "出现对 AssistantBall 内部符号的直接调用——违反双线解耦契约"
    )


def test_writes_only_via_act_family():
    """配置写唯一通道＝/api/assistant/act 家族 + agent/plan（零副作用）。
    P3 追加两个**非配置**生成端点：transcribe（语音说目标→文本回填）与
    tts-test（小智播报试听，fast 档）。出现此外的 /api/ POST 目标＝有人
    绕开确认/撤销/审计三板斧，先红再谈。"""
    src = _src()
    urls = set(re.findall(r"post\('([^']+)'", src))
    allowed = {"/api/assistant/act", "/api/assistant/act/undo",
               "/api/assistant/agent/plan",
               "/api/assistant/transcribe", "/api/voice/tts-test",
               # P4 配对管理（会话签发/踢下线，非配置写）
               "/api/assistant/pair", "/api/assistant/pair/revoke"}
    assert urls <= allowed, f"越界写通道：{urls - allowed}"


def test_loader_init_shell_matches_template():
    for tpl, shell in zip(TEMPLATES, ("admin", "workspace")):
        text = tpl.read_text(encoding="utf-8")
        block = re.search(
            r"assistant-agent\.js\?v=[0-9a-z]+.*?XZAgent\.init\(\{.*?shell:'([a-z]+)'",
            text,
            flags=re.S,
        )
        assert block, f"{tpl.name} 缺 XZAgent.init"
        assert block.group(1) == shell
