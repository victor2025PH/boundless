# -*- coding: utf-8 -*-
"""N-4（#250 = #215③ 第二次；#161 族）：语音质量闸——「已核有声」与「疑似无声」别再同屏。

事实（钧机 1.0.76，群图 mid 1258 / 1315 + 诊断包 TAZ9QS / TWJ7C8）：Mizuki 克隆声两天两次
生成，服务端终审全部 ``speech=voiced basis=transcript+energy``（Whisper 转出全文，CER≈0），
面板却同屏三条：「实际使用 … ✓ 已核有声」+ 红条「音频异常（疑似无声）」+ 橙条「试听已过期」，
发送灰。真因两层：
  ① ``cp-voice.js`` 的红条 / 橙条靳靠 ``hidden`` 属性隐藏，而 0831 加的 ``.pv-note{display:flex}``
     是作者样式、盖过 UA 的 ``[hidden]{display:none}`` → 两条 banner 从 8-31 起恒常可见
     （Playwright 实测 hidden=true 仍 display:flex / 27px）；现有门禁只断言 ``el.hidden``；
  ② 「跟随翻译发声」auto → 服务端解析成 zh、与原文同语种不译（``voice_translated=false``）
     → 前端把基准记成 'auto'、当前有效值却是 conv_lang 'zh' → 生成一结束就「过期」（C 项修）。

本文件钉 B 项（单一判定）：
- 服务端：一段音频只出一个结论——``verdict_label`` 四档映射、``format_tts_verdict_line`` 一行
  ``[tts] verdict=…`` 固定字段（persona backend voice dur speech energy cer verdict）；
  tts-test 回包 ``voice_meta.verdict`` 与日志同源；旧三条 ``[voice/tts-test]`` 各说各话的行退场。
- 前端：结果区只有一个结论元素 ``[data-role="verdict"]``（四态 stale/blocked/ok/unverified）；
  ``silent-note`` / ``stale-note`` 两个独立 banner 不复存在；``_metaLine`` 不再另下「已核有声」；
  shadow 样式表首条 ``[hidden]{display:none !important}`` 钉死病根①；``panelVerdict`` 纯函数
  由 ``desktop/test/cp-voice-speech-verdict.test.js`` 穷举四态（本文件顺带 node 直跑）。
- 人设页「音色体检」行按同一 ``voice_meta.verdict`` 回填（N-2 留的钩子），不再各写一套判定。
- 安全语义不放宽：silent / garbled 仍阻发；unknown 只提醒先听。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.ai.speech_verdict import (
    VERDICT_BY_SPEECH,
    format_tts_verdict_line,
    speech_verdict,
    verdict_label,
)

_ROOT = Path(__file__).resolve().parents[1]
_VOICE_JS = _ROOT / "shared" / "copilot" / "components" / "cp-voice.js"
_I18N_JS = _ROOT / "shared" / "copilot" / "i18n" / "cp-i18n.js"
_ROUTES = _ROOT / "src" / "web" / "routes" / "voice_routes.py"
_PERSONAS = _ROOT / "src" / "web" / "templates" / "personas.html"
_NODE_TEST = _ROOT / "desktop" / "test" / "cp-voice-speech-verdict.test.js"

# 钧机 09-07 18:30:19 那次终审的形状（TWJ7C8）：中文轨、34 字全文命中
_JUN_SV = {"cer": 0.06, "retried": 0, "hyp_chars": 34}


def _js() -> str:
    return _VOICE_JS.read_text(encoding="utf-8")


def _seg(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


# ── 服务端：一段音频一个结论 ────────────────────────────────────────────────────

def test_verdict_label_four_states_only():
    assert set(VERDICT_BY_SPEECH.values()) == {"ok", "block:silent", "block:garbled", "unverified"}
    assert verdict_label("voiced") == "ok"
    assert verdict_label("VOICED ") == "ok"
    assert verdict_label("silent") == "block:silent"
    assert verdict_label("garbled") == "block:garbled"
    assert verdict_label("unknown") == "unverified"
    assert verdict_label("") == "unverified" and verdict_label(None) == "unverified"
    assert verdict_label("weird") == "unverified", "未知取值宁可让坐席先听一遍"


def test_jun_shape_same_audio_yields_single_ok_verdict():
    """钧机形态：转写命中 + 能量在场 → voiced；同一份证据不可能同时产出「无声」。"""
    v = speech_verdict(_JUN_SV, False)
    assert v["speech"] == "voiced" and v["basis"] == "transcript+energy"
    assert verdict_label(v["speech"]) == "ok"
    # 缓存命中零回验（13:52:51 形态）：只有能量 → 仍是 voiced，不是「疑似无声」
    v2 = speech_verdict(None, False)
    assert v2["speech"] == "voiced" and v2["basis"] == "energy"
    assert verdict_label(v2["speech"]) == "ok"


def test_safety_semantics_not_relaxed():
    """真无声 / 真乱音仍是阻发档（#161 的教训是乱音放行了）。"""
    assert verdict_label(speech_verdict(None, True)["speech"]) == "block:silent"
    garbled = speech_verdict({"cer": 0.85, "retried": 1, "hyp_chars": 12}, False)
    assert garbled["speech"] == "garbled"
    assert verdict_label(garbled["speech"]) == "block:garbled"
    assert verdict_label(speech_verdict(None, None)["speech"]) == "unverified"


def test_format_tts_verdict_line_fixed_fields_and_prefix():
    sp = speech_verdict(_JUN_SV, False)
    line = format_tts_verdict_line(
        sp, persona="mizuki", backend="avatar_clone", voice="zh-CN-XiaoxiaoNeural",
        duration_sec=7.04, synth_verify=_JUN_SV, file="ttspreview-581492e558.wav")
    assert line.startswith("[tts] verdict=ok ")
    # 字段顺序固定：persona backend voice dur speech energy cer verdict（verdict 在最前作为抓手）
    keys = re.findall(r"(\w+)=", line)
    assert keys[:9] == ["verdict", "persona", "backend", "voice", "dur", "speech", "energy",
                        "cer", "basis"], keys
    assert "persona=mizuki backend=avatar_clone voice=zh-CN-XiaoxiaoNeural dur=7.0 " in line
    assert "speech=voiced energy=ok cer=0.06 basis=transcript+energy transcript_chars=34" in line
    assert "file=ttspreview-581492e558.wav stage=preview" in line


def test_format_tts_verdict_line_missing_values_are_dashes():
    line = format_tts_verdict_line({"speech": "silent", "energy": "silent"}, persona="",
                                   backend=None, voice="", duration_sec="x", file="")
    assert "verdict=block:silent" in line
    assert "persona=- backend=- voice=- dur=- speech=silent energy=silent cer=- basis=- " in line
    assert "transcript_chars=0 file=- stage=preview" in line
    assert format_tts_verdict_line(None).startswith("[tts] verdict=unverified ")


def test_tts_test_route_emits_single_verdict_line_and_meta_verdict():
    src = _ROUTES.read_text(encoding="utf-8")
    body = _seg(src, "async def _run_tts_preview(", "@app.post(\"/api/voice/tts-test\")")
    assert "format_tts_verdict_line(" in body
    assert 'stage="preview"' in body
    assert '"verdict": _verdict_label(_speech.get("speech"))' in body, "回包与日志同源"
    # 旧三条各说各话的行退场（同一次终审不再 INFO/WARNING 三种口径）
    for old in ("预览产物服务端判无声", "判念错且改派 Edge 未成功", "[voice/tts-test] 有声终审"):
        assert old not in body, old
    # silent / garbled 仍走 WARNING（取证时按级别过滤仍抓得到）
    assert 'if _speech.get("speech") in ("silent", "garbled"):' in body
    assert 'logger.warning("%s", _vline)' in body


# ── 前端：结果区唯一结论行 + hidden 病根 ─────────────────────────────────────────

def test_shadow_css_makes_hidden_authoritative():
    css = _seg(_js(), "_css() {", "set client(c)")
    assert "[hidden] { display:none !important; }" in css, "hidden 必须压过任何作者 display 规则"
    # 必须在 .pv-note / .effline 这些 display:flex 规则之前（顺序不影响 !important，但便于读）
    assert css.index("[hidden] { display:none !important; }") < css.index(".pv-note {")
    assert ".pv-verdict" in css


def test_result_zone_has_exactly_one_verdict_element_and_no_dual_banners():
    js = _js()
    render = _seg(js, "async _genTts() {", "static selectionMismatch(")
    assert render.count('<div class="pv-verdict" data-role="verdict"') == 1
    assert 'data-role="silent-note"' not in js and 'data-role="stale-note"' not in js, \
        "红条 / 橙条两个独立 banner 不得再出现（钧机三条同屏的载体）"
    assert 'data-detector="v4-single"' in render
    # 过期时的出路是主按钮「重新生成」，不是灰掉的发送
    assert 'data-role="regen-main" hidden' in render
    # 实际使用行只报事实：不再 push 「已核有声」chip
    meta = _seg(js, "_metaLine(d) {", "async _loadProfiles()")
    assert "m_speech_ok" not in meta and 'm.speech === "voiced"' not in meta


def test_sync_notes_is_single_exit_over_panel_verdict():
    fn = _seg(_js(), "_syncNotes() {", "_warnBeacon(kind) {")
    assert "CpVoice.panelVerdict({" in fn
    assert 'vEl.setAttribute("data-state", v.state)' in fn
    assert "send.hidden = stale" in fn and "regen.hidden = !stale" in fn
    assert "fbEl.hidden = this._silent || stale" in fn, "回落说明让位于结论（单条出口）"
    # 结论行只由这一处写：其它地方不得再直接翻 hidden
    js = _js()
    assert js.count(".hidden = false") == 1 or "vEl.hidden = false" in fn


def test_panel_verdict_pure_function_shape():
    fn = _seg(_js(), "static panelVerdict(o) {", "static speechDecision(")
    for state in ('state: "stale"', 'state: "blocked"', 'state: "ok"', 'state: "unverified"'):
        assert state in fn, state
    # 过期优先于一切；阻发文案按证据来源分（服务端确认 vs 客户端疑似）
    assert fn.index('state: "stale"') < fn.index('state: "blocked"') < fn.index('state: "ok"')
    assert '(speech === "silent" || speech === "garbled") ? speech : "client"' in fn


def test_i18n_keys_present_in_zh_and_en_with_same_placeholders():
    txt = _I18N_JS.read_text(encoding="utf-8")
    for key in ("cp.voice.v_ok", "cp.voice.v_ok_t", "cp.voice.v_unverified", "cp.voice.stale_note",
                "cp.voice.silent_note", "cp.voice.silent_note_server", "cp.voice.garbled_note"):
        hits = re.findall(r'"%s":\s*"((?:[^"\\]|\\.)*)"' % re.escape(key), txt)
        assert len(hits) >= 2, f"{key} 需 zh + en 两份"
        assert set(re.findall(r"\{(\w+)\}", hits[0])) == set(re.findall(r"\{(\w+)\}", hits[1])), key
    zh = _seg(txt, '"cp.voice.v_ok":', '"cp.voice.v_unverified":')
    assert "✅" in zh and "可发送" in zh
    # 阻发三条统一 ❌ 且都给出路「重新生成」；客户端兜底那条必须说明「疑似 / 服务端也没拿到证据」
    zh_all = _seg(txt, '"cp.voice.silent_note":', '"cp.voice.std_voice_note":')
    assert zh_all.count("❌") == 3 and zh_all.count("重新生成") >= 2
    assert "服务端也没拿到证据" in zh_all


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不在 PATH")
def test_node_pure_gate_passes():
    """desktop/test 门禁穷举 panelVerdict 四态（与 speechDecision / selectionMismatch 同文件）。"""
    r = subprocess.run(["node", str(_NODE_TEST)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    assert "passed" in r.stdout


# ── 人设页体检行：同源回填 ───────────────────────────────────────────────────────

def test_persona_page_voice_check_row_renders_same_verdict_source():
    html = _PERSONAS.read_text(encoding="utf-8")
    fn = _seg(html, "function _peVqVerdictKey(vm){", "async function peVcUnbind(){")
    assert "vm.verdict" in fn and "'psn_vq_aud_ok'" in fn and "'psn_vq_aud_silent'" in fn
    assert "'psn_vq_aud_garbled'" in fn and "'psn_vq_aud_unverified'" in fn
    assert "getElementById('pe-vq-audition')" in fn
    assert "document.addEventListener('persona-voice-audition'" in fn
    # 只渲染当前编辑的人设（换抽屉重读，不串人设）
    assert "!== String(_editingProfileId || '')) return;" in fn
    assert "peVqRenderAudition((p && p.id) || _editingProfileId || '')" in html
    from src.web.i18n_packs.persona_studio import EN, ZH
    for k in ("psn_vq_aud_ok", "psn_vq_aud_silent", "psn_vq_aud_garbled", "psn_vq_aud_unverified"):
        assert ZH.get(k) and EN.get(k), k
        assert set(re.findall(r"\{(\w+)\}", ZH[k])) == set(re.findall(r"\{(\w+)\}", EN[k])), k
