# -*- coding: utf-8 -*-
"""WP-7 前端半件（新手任务悬浮卡）接线门禁（2026-08-18）。

组件是自包含静态 JS（刻意不进 unified_inbox 侧栏——那是 sidebar-dock 线的
活跃施工区），本门禁钉住跨文件契约：
1. 壳 include 在位且带 ?v= 缓存戳；
2. i18n pack 双语镜像 + ``agt_task_<id>`` 与后端 TASK_IDS 一一对应；
3. JS 里 window.T 引用的每个键都在 pack（无中文兜底架构，缺键=渲染裸键名）；
4. JS 零 CJK 字面量（文案全键化）；
5. 完成匹配表与 5 个权威 API 的路径契约钉住（send-voice/goals 必须精确段
   匹配——``send-voice-status`` / ``/api/goals/{id}/...`` 误中会把任务乱勾）。
"""
from __future__ import annotations

import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
JS = ENGINE / "src" / "web" / "static" / "workspace" / "agent-tasks-card.js"
SHELL = ENGINE / "src" / "web" / "templates" / "workspace_base.html"


def test_shell_include_with_cache_stamp():
    src = SHELL.read_text(encoding="utf-8")
    m = re.search(r'src="/static/workspace/agent-tasks-card\.js\?v=(\w+)"', src)
    assert m, "workspace_base 未挂 agent-tasks-card.js（或缺 ?v= 缓存戳）"


def test_pack_bilingual_and_task_ids_aligned():
    from src.utils.agent_tasks import TASK_IDS
    from src.web.i18n_packs.agent_tasks_card import EN, ZH

    assert set(ZH.keys()) == set(EN.keys()), "zh/en 键集不镜像"
    for tid in TASK_IDS:
        assert f"agt_task_{tid}" in ZH, f"任务 {tid} 缺文案键"
    extra = {k for k in ZH if k.startswith("agt_task_")} - {
        f"agt_task_{t}" for t in TASK_IDS}
    assert not extra, f"文案存在幽灵任务键: {extra}"


def test_js_t_keys_all_defined_and_no_cjk():
    from src.web.i18n_packs.agent_tasks_card import ZH
    from src.utils.agent_tasks import TASK_IDS

    src = JS.read_text(encoding="utf-8")
    # 与引擎密封页门禁同口径：剥注释后扫（注释不渲染给用户，中文注释合法）。
    # 尾注释规则要求「空白+//+空白」——字符串里的 '://'（前邻是冒号）不会误伤。
    stripped = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    stripped = re.sub(r"(?m)^\s*//.*$", "", stripped)
    stripped = re.sub(r"(?m)\s//\s.*$", "", stripped)
    assert not re.search(r"[\u4e00-\u9fff]", stripped), \
        "JS 可执行体出现 CJK 字面量（应全键化）"
    static_keys = set(re.findall(r"T\('(agt_[a-z_]+)'\)", src))
    for k in static_keys:
        assert k in ZH, f"JS 引用未定义键 {k}"
    # 动态拼接键 agt_task_ + t.id：逐 id 验证
    if "agt_task_' + t.id" in src or "'agt_task_' + t.id" in src:
        for tid in TASK_IDS:
            assert f"agt_task_{tid}" in ZH


def test_match_table_pins():
    src = JS.read_text(encoding="utf-8")
    assert "path === '/api/unified-inbox/send-voice'" in src, \
        "send_voice 必须精确段匹配（防 send-voice-status 误中）"
    assert "path === '/api/goals'" in src, \
        "create_goal 必须精确匹配（防 /api/goals/{id}/* 子路径误中）"
    assert re.search(r"\\/api\\/drafts\\/\[\^/\]\+\\/resolve", src.replace("^", "^")) \
        or "/^\\/api\\/drafts\\/[^/]+\\/resolve$/" in src, "resolve 匹配缺失"
    assert "path === '/api/unified-inbox/thread'" in src
    assert "'agt_'" in src, "ui-event 埋点前缀 agt_ 缺失"
    # 旁路安全：不得 clone/消费响应体（原 promise 必须原样返回）
    assert ".clone(" not in src and "res.json(" not in src.replace(
        "r.json()", ""), "拦截器不得消费业务响应体"
