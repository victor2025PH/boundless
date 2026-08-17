# -*- coding: utf-8 -*-
"""右栏「语音克隆/发送」cp-voice 状态机收口的静态接线门禁（P0 2026-08-05）。

修的是坐席实录两连抱怨 + 一个更重的隐患：
  ① 生成过的试听赖在面板里清不掉（预览只在切会话时消失，无清除按钮，发送不复位）；
  ② 想再来一条找不到生成按钮（「🎙️ 语音」是名词、无重新生成入口、引导语被状态顶掉）；
  ③ 右栏发送链无幂等键无在途闸门——连点两下真的给客户发两条、烧两份 TTS
     （send_dedup.reserve 对空 client_msg_id 直接放行；主输入框链早已带键）。

交互时序行为由真浏览器门禁 tools/verify_cp_voice_ui.py 压（27 断言）；
本文件守「写了没挂线/只改一棵树/词条缺半边/版本戳没 bump」这类静态回归。
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]

_TREES = (
    REPO / "shared" / "copilot",
    REPO / "desktop" / "renderer" / "shared" / "copilot",
)

# 本批次最低缓存戳：改 cp-voice.js 必须 bump ?v=。共享树上多条线同日各自推进
# 字母位（实测本批 c 落地 30 分钟内就被并行线推到 d），钉「恰好等于」会误伤
# 合法的后续批次——断言语义是**不回退**（≥ 本批），回退到修复前旧戳才红。
_STAMP_FLOOR = (20260805, "c")
_STAMP_RE = re.compile(r"cp-voice\.js\?v=(\d{8})([a-z]?)")


def _assert_stamp_at_least(html: str, where) -> None:
    m = _STAMP_RE.search(html)
    assert m, f"{where} 缺 cp-voice.js 版本戳"
    got = (int(m.group(1)), m.group(2) or "")
    assert got >= _STAMP_FLOOR, f"{where} 版本戳回退：{got} < {_STAMP_FLOOR}"


def _both(rel: str):
    for base in _TREES:
        p = base / rel
        yield p, p.read_text(encoding="utf-8")


def test_cp_voice_state_machine_anchors_in_both_trees():
    for p, js in _both("components/cp-voice.js"):
        # 清除/重新生成入口（报障①②的修复主体）
        assert 'data-act="clear-preview"' in js, p
        assert 'cp.voice.regen_btn' in js, p
        assert 'data-role="gen-main"' in js, p
        # 在途互斥 + 过期守卫 + 代际防串（时序行为浏览器门禁压，这里钉存在性）
        assert "_setBusy" in js and "_isStale" in js and "_syncStale" in js, p
        assert "_clearPreview" in js, p
        assert "this._epoch" in js, p
        # 幂等键：与主输入框语音发送同口径（空键=服务端 dedup 直接放行）
        assert 'client_msg_id: "cpv-"' in js, p
        # 发送成功必须复位（清预览+清文字）并广播给宿主
        assert "cp-voice-sent" in js, p
        # P1 所听即所发：发送带回试听产物名（服务端校验后复用，零二次合成）
        assert "preview_filename: this._previewFilename" in js, p
        # P1 取消生成（epoch 代际作废）+ 字数计（前端先拦超限）
        assert 'data-act="cancel-gen"' in js, p
        assert '_cancelGen' in js and "_syncCounter" in js, p
        assert 'data-role="cnt"' in js, p


def test_cp_voice_preserves_sibling_line_contracts_in_both_trees():
    """同日并行线的两个契约不许被本批次冲掉：
    试听带会话上下文（试听=发送 契约）+ __system__ 系统音色三档语义。"""
    for p, js in _both("components/cp-voice.js"):
        assert "chat_key: c.chatKey || undefined" in js, p
        assert '"__system__"' in js, p


def test_cp_i18n_new_keys_bilingual_in_both_trees():
    keys = (
        "cp.voice.gen_busy_btn", "cp.voice.gen_wait",
        "cp.voice.regen_btn", "cp.voice.regen_t", "cp.voice.clear_t",
        "cp.voice.retry_btn", "cp.voice.sending",
        "cp.voice.stale_note", "cp.voice.fallback_warn",
        "cp.voice.cancel_btn", "cp.voice.too_long",
    )
    for p, js in _both("i18n/cp-i18n.js"):
        for k in keys:
            # zh/en 双字典各出现一次 → 每键至少 2 次
            assert js.count(f'"{k}"') >= 2, f"{p} 缺双语词条 {k}"


def test_generate_button_is_verb_in_both_langs():
    """「🎙️ 语音」名词按钮是「找不到生成按钮」报障的直接根因，钉住动词化文案。

    P2B（2026-08-08）图标语言统一：🎙️ 从 i18n 值迁出，由组件 _genLabelHtml() 渲染
    线性 SVG（mic）——动词化契约不变（精确钉住），图标与文案各归其位。"""
    for p, js in _both("i18n/cp-i18n.js"):
        assert '"cp.voice.tts_btn": "生成语音"' in js, p
        assert '"cp.voice.tts_btn": "Generate voice"' in js, p
        # 图标必须由组件渲染（防「值里删了 emoji、组件也没图标」的双失守）
    for p, js in _both("components/cp-voice.js"):
        assert "_genLabelHtml" in js and '_ic("mic"' in js, p


def test_host_consumes_cp_voice_sent_and_stamp_bumped():
    html = (REPO / "src" / "web" / "templates" / "unified_inbox.html"
            ).read_text(encoding="utf-8")
    # 宿主必须消费 cp-voice-sent（此前事件空放：发完消息流不刷新，坐席以为没发出去）
    assert "addEventListener('cp-voice-sent'" in html
    assert "__cpVoiceSentBound" in html
    # 改 cp-voice.js 必须 bump 缓存戳（不回退语义，见 _STAMP_FLOOR 注释）
    _assert_stamp_at_least(html, "unified_inbox.html")


def test_app_shell_stamp_bumped_in_both_trees():
    for p, html in _both("app.html"):
        _assert_stamp_at_least(html, p)


def test_preview_reuse_wired_on_server():
    """「所听即所发」接线门禁（写了没挂线）：试听侧登记 sidecar、发送侧校验复用、
    观测三暴露面（avatar-status / workspace metrics / ops 卡行）。
    行为语义由 tests/test_tts_preview_reuse.py + 浏览器门禁压。"""
    vr = (REPO / "src" / "web" / "routes" / "voice_routes.py").read_text(encoding="utf-8")
    assert "record_preview_meta" in vr
    assert "reuse_stats_snapshot" in vr          # avatar-status 暴露面
    sr = (REPO / "src" / "web" / "routes" / "unified_inbox_send_routes.py"
          ).read_text(encoding="utf-8")
    assert "resolve_reusable_preview" in sr
    assert '"reused_preview"' in sr
    dr = (REPO / "src" / "web" / "routes" / "drafts_routes.py").read_text(encoding="utf-8")
    assert '"voice_preview_reuse"' in dr         # workspace metrics 暴露面
    ops = (REPO / "src" / "web" / "templates" / "ops_overview.html"
           ).read_text(encoding="utf-8")
    assert "ov2_av_reuse" in ops and "preview_reuse" in ops   # ops 卡行
