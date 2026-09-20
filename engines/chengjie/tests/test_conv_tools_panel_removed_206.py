# -*- coding: utf-8 -*-
"""#206 补证（M-4 D，D-M7，2026-09-06 SJTRJY / 52RAGZ 用户确认）：右侧工具箱「会话工具」整块删除。

事实：「会话」折叠区是空壳；标签 / 归档 / 删除与会话行右键 · ⋯ 菜单完全重复，且两个「删除」
（灰 + 红）语义不明；「+加标签」下拉被卡体容器裁切。修法＝整块删除；「全自动运行中」状态卡
迁入顶部「AI 状态」弹层；认领按钮回会话头部；标签 / 搁置 / 归档 / 清空记录 / 删除会话只留
会话行菜单一处（标签编辑早已是浮层，遮挡随之消失）；装配清单 convops 收成 app 独有。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TPL = REPO / "src" / "web" / "templates" / "unified_inbox.html"
HTML = TPL.read_text(encoding="utf-8")


def _between(src: str, a: str, b: str) -> str:
    i = src.index(a)
    return src[i:src.index(b, i)]


def test_conv_tools_card_gone_from_toolbox():
    assert 'id="conv-ops"' not in HTML
    assert 'data-cp-card="convops"' not in HTML
    assert 'data-cp-toggle="convops"' not in HTML
    for dead in ('id="conv-delete-btn"', 'id="archive-btn"', 'id="snooze-btn"',
                 'id="hdr-tags-row"', 'id="convops-pill"', 'class="conv-ops-actions"'):
        assert dead not in HTML, dead
    # 工具箱 tools 页签只剩：翻译 / 语音克隆发送 / 人设绑定 / AI 生成图片（M-5 C 处置）/ 养号 / 已隐藏的分析
    tools = _between(HTML, '<div class="ws-cp-sec" data-tab="tools">', 'id="mob-plat-bar"')
    cards = re.findall(r'data-cp-card="([\w-]+)"', tools)
    assert "convops" not in cards
    for keep in ("xlate", "voice", "persona", "image"):
        assert keep in cards, keep


def test_auto_status_card_moved_into_ai_status_layer():
    ov = _between(HTML, 'id="ai-diag-overlay"', 'id="ai-diag-body"')
    assert 'id="auto-safety-bar"' in ov and 'id="auto-safety-stats"' in ov
    for act in ('onclick="_openAutoLog()"', 'id="asb-tune-link"', 'onclick="pauseAutoMode()"'):
        assert act in ov, act
    assert HTML.count('id="auto-safety-bar"') == 1
    # 打开弹层即按档位刷显隐与今日统计
    body = _between(HTML, "async function openAiDiag(){", "async function _refreshConvStatus(c){")
    assert "_syncAutoUi();" in body
    # 既有接线未动：_syncAutoUi 仍按 mode-select 控 #auto-safety-bar，_loadAutoStats 仍填 #auto-safety-stats
    assert "var bar=document.getElementById('auto-safety-bar');" in HTML
    assert "var el=document.getElementById('auto-safety-stats');" in HTML


def test_claim_button_back_in_chat_header_with_seat_gate():
    acts = _between(HTML, '<div class="chat-header-acts">', 'id="mode-select"')
    assert 'id="claim-hdr-btn"' in acts
    assert "ws_multi_seat is not defined or ws_multi_seat" in acts
    assert HTML.count('id="claim-hdr-btn"') == 1


def test_row_menu_is_the_single_home_for_conv_ops():
    menu = _between(HTML, "function _convCtxOpenForKey(key, anchorEl, x, y){", "/* ── 消息级操作")
    for key in ("inbox.snooze.btn_short", "inbox.conv.tag", "inbox.conv.archive",
                "inbox.ctx.clear", "inbox.ctx.del_conv"):
        assert key in menu, key
    # 面板里语义不明的裸「删除」按钮没了；行菜单两项各有明确名字（清空聊天记录 / 删除会话）
    from src.web.i18n_packs.inbox_msg_ops import EN, ZH
    assert ZH["inbox.ctx.del_conv"] == "删除会话" and ZH["inbox.ctx.clear"] == "清空聊天记录"
    assert EN["inbox.ctx.del_conv"] and EN["inbox.ctx.clear"]
    # 快捷键 E / S 仍指向既有函数（归档 / 搁置），函数本体保留
    assert "async function toggleArchive(){" in HTML and "function snoozeConv(){" in HTML


def test_toolbox_copy_and_group_title_no_longer_promise_conv_ops():
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    assert ZH["inbox.cpg.tools.session"] != "会话工具"
    assert "会话运维" not in ZH["inbox.cpg.tools.s2"]
    assert "菜单" in ZH["inbox.cpg.tools.s2"]
    assert "menu" in EN["inbox.cpg.tools.s2"].lower()
    assert 'data-i18n="inbox.cpg.tools.session"' in HTML   # 组标题仍在（语音克隆 / 人设绑定归它）


def test_panel_manifest_convops_is_app_only_and_trees_in_sync():
    a = REPO / "shared" / "copilot" / "panel-manifest.js"
    b = REPO / "desktop" / "renderer" / "shared" / "copilot" / "panel-manifest.js"
    sa, sb = a.read_bytes(), b.read_bytes()
    assert sa == sb, "双树 panel-manifest.js 必须字节一致"
    literal = sa.decode("utf-8").split("var MANIFEST =", 1)[1].split("\n;", 1)[0]
    m = json.loads(literal)
    conv = [c for c in m["cards"] if c["id"] == "convops"]
    assert conv and conv[0]["surfaces"] == ["app"], conv
    assert "#206" in conv[0].get("note", "")
