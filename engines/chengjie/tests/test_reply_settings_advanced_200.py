# -*- coding: utf-8 -*-
"""自动回复设置·高级三块 + 平台覆写表（L-4 E，#200，2026-09-06）门禁。

PF5QAF / M43ZWH 实录：语音「启用开关 + 触发方式」双层互相打架；缓冲话术硬编码中文提示藏在
高级组；人设节奏表要填人设 ID；平台专家表默认展开且 LINE 打字格可选。钉住（模板静态接线 +
后端契约不变）：
1. 语音：单一下拉 #rps-voice-mode（关闭/总是/对方发语音才回/智能）驱动隐藏的底层两键；
2. 缓冲语：并入「AI 接管」卡（#rps-holding-row），随拟稿人审出现，本体 #rps-holding 保留；
3. 人设覆写表删除，rpsPoCollect 无表时回传既有覆写（保存不改动它们）；
4. 平台专家表折叠（details）+「跟随全局（实际值）」+ LINE 打字硬禁。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TPL = (REPO / "src" / "web" / "templates" / "reply_settings.html").read_text(encoding="utf-8")


def test_voice_single_dropdown_drives_hidden_keys():
    assert 'id="rps-voice-mode"' in TPL and 'onchange="rpsVoiceModeChange(this)"' in TPL
    # 四档：关闭 + 三种触发（不再单列「从不」）
    seg = TPL.split('id="rps-voice-mode"', 1)[1].split("</select>", 1)[0]
    assert [m for m in re.findall(r'value="([a-z_]+)"', seg)] == ["off", "always", "when_peer_voice", "smart"]
    # 底层两键仍在（隐藏），后端契约与脏检测零改动
    assert '<input type="checkbox" id="rps-voice" hidden' in TPL
    assert '<select id="rps-voice-trigger" hidden' in TPL
    assert '{path: "inbox.l2_autosend.voice.enabled", kind: "check", el: "rps-voice"}' in TPL
    assert '{path: "inbox.l2_autosend.voice.trigger", kind: "select", el: "rps-voice-trigger"' in TPL
    # off 只翻 enabled（trigger 保留 → 日志区分 disabled 与 never）
    fn = TPL.split("function rpsVoiceModeChange(sel){", 1)[1].split("}\nwindow.rpsVoiceModeChange", 1)[0]
    assert "if (mode === 'off'){ en.checked = false; }" in fn
    assert "en.checked = true; tr.value = mode;" in fn
    assert "rpsVoiceModeSync();" in TPL.split("function rpsFill(values){", 1)[1].split("rpsPaceRefresh();", 1)[0]
    assert 'id="rps-voice-engine"' in TPL and "/api/voice/effective-config" in TPL


def test_holding_moved_into_master_card_and_gated_by_review_mode():
    master = TPL.split('id="rps-sec-gates"', 1)[1].split('id="rps-sec-am"', 1)[0]
    assert 'id="rps-holding-row"' in master and 'id="rps-holding-ui"' in master
    assert 'id="rps-holding" hidden' in TPL, "本体保留承接旧 id"
    assert "rpsHoldingRowSync(mode);" in TPL.split("function rpsMasterRender(mode){", 1)[1].split("}\n", 1)[0] + "}\n" \
        or "rpsHoldingRowSync(mode);" in TPL
    sync = TPL.split("function rpsHoldingRowSync(mode){", 1)[1].split("}\n\n", 1)[0]
    assert "(m === 'suggest' || m === 'custom')" in sync
    # 提示不再写死中文「稍等我看看哈~」
    assert "稍等我看看哈~" not in TPL


def test_persona_override_table_removed_but_overrides_preserved():
    assert 'id="rps-po-body"' not in TPL and 'onclick="rpsPoAdd()"' not in TPL
    assert 'id="rps-po-pointer"' in TPL and 'href="/personas"' in TPL
    collect = TPL.split("function rpsPoCollect(){", 1)[1].split("return out;", 1)[0]
    assert "if (!document.getElementById('rps-po-body')) return JSON.parse(JSON.stringify(rpsPoStored || {}));" in collect
    assert 'out[RPS_PO_KEY] = rpsPoCollect();' in TPL, "保存仍回传人设覆写键（不改动既有值）"


def test_platform_expert_table_folded_with_effective_values_and_line_typing_disabled():
    card = TPL.split('id="rps-sec-plat"', 1)[1].split('id="rps-sec-style"', 1)[0]
    assert '<details class="rps-guard-adv" id="rps-plat-fold">' in card
    assert "open" not in card.split("<details", 1)[1].split(">", 1)[0], "默认折叠"
    assert "RPS_PLAT_KNOWN_UNSUPPORTED = {line: {typing: true}}" in TPL
    assert "function rpsFollowLabel(val)" in TPL and "poFollowVal" in TPL
    row = TPL.split("function rpsPlatRow(p){", 1)[1].split("function rpsPlatRender(){", 1)[0]
    assert "rpsFollowLabel(gMin)" in row and "rpsFollowLabel(gMax)" in row
    assert "RPS_PLAT_KNOWN_UNSUPPORTED[p] && RPS_PLAT_KNOWN_UNSUPPORTED[p][key]" in row


def test_i18n_bilingual_new_keys():
    from src.web.i18n_packs.reply_settings_page import EN, ZH
    for k in ("rps_voice_mode", "rps_vm_off", "rps_voice_mode_hint", "rps_voice_engine_on",
              "rps_voice_engine_off", "rps_voice_risk_unreachable", "rps_holding_label2",
              "rps_holding_hint2", "rps_po_moved", "rps_po_kept", "rps_po_follow_val",
              "rps_plat_sum", "rps_plat_sum_none"):
        assert ZH.get(k) and EN.get(k), f"{k} 缺双语"
