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


def test_cp_voice_voice_xlate_wired_in_both_trees():
    """P0-V2b（2026-08-30）右栏译声接线：后端译声（P0-V2 2026-08-19）落地两天后
    composer 语音入口被 B26 撤除，而右栏 cp-voice（唯一可见语音入口）从未接
    target_lang——「打中文发目标语克隆声」对用户等于不存在（实录报障）。本门禁
    钉住整条链：组件带目标语+译稿回显+过期含语言维度 → client/桌面 IPC 透传 →
    effective-config 回传会话语言（生成前预告）。"""
    for p, js in _both("components/cp-voice.js"):
        assert "target_lang: xlt || undefined" in js, p            # 试听带目标语
        assert "target_lang: this._xlTarget() || undefined" in js, p  # 发送带目标语
        assert 'data-role="xlfollow"' in js, p                     # 跟随翻译开关
        assert '"aitr.voicexl"' in js, p                           # 与主输入框同键互通
        assert "voice_translated" in js and "spoken_text" in js, p  # 译稿行（所见即所念）
        assert "_previewXl" in js, p                               # 过期判定含语言维度
        assert "cp-voice-xl-changed" in js, p                      # 开关翻转广播宿主
        assert '"conv_lang" in d' in js, p                         # 预告特性探测（旧后端隐藏）
        assert "__cpVoiceXlPref" in js, p                          # 显式目标语优先于 auto（P0-V2c）
        # P0（2026-08-31「日文怪声」）：目标语超出音色引擎能力 → 生成前警示
        assert "_langUnsupported" in js, p
        assert "voice_langs" in js, p
    for p, js in _both("client/copilot-client.js"):
        assert js.count("target_lang") >= 2, p   # web 适配器解构+body 双处透传
    mj = (REPO / "desktop" / "main.js").read_text(encoding="utf-8")
    assert mj.count("target_lang") >= 2, "desktop IPC voice-tts 未透传 target_lang"
    vr = (REPO / "src" / "web" / "routes" / "voice_routes.py").read_text(encoding="utf-8")
    assert '"conv_lang"' in vr, "effective-config 缺 conv_lang（生成前预告数据源）"
    assert '"voice_langs"' in vr, "effective-config 缺 voice_langs（语言能力守卫数据源）"


def test_cp_voice_xlate_keys_bilingual_in_both_trees():
    keys = (
        "cp.voice.xl_follow", "cp.voice.xl_follow_t", "cp.voice.xl_to",
        "cp.voice.xl_unknown", "cp.voice.xl_off", "cp.voice.xl_yue_warn",
        "cp.voice.xl_engine_unsupported",
        "cp.voice.spoken_as", "cp.voice.sent_xl", "cp.voice.gen_wait_est",
    )
    for p, js in _both("i18n/cp-i18n.js"):
        for k in keys:
            assert js.count(f'"{k}"') >= 2, f"{p} 缺双语词条 {k}"


def test_cp_voice_humane_labels_bilingual_in_both_trees():
    """P0-2（2026-08-31）：情绪/后端内部值不得裸奔给坐席——emo_* 词条双语齐备、
    后端标签收敛为「人设克隆声 / 标准语音」两个用户概念（内部管道名不外漏）。"""
    emo_keys = tuple(
        f"cp.voice.emo_{e}" for e in (
            "neutral", "warm", "happy", "excited", "playful", "empathetic",
            "apologetic", "calm", "sad", "serious", "angry", "gentle"))
    for p, js in _both("i18n/cp-i18n.js"):
        for k in emo_keys:
            assert js.count(f'"{k}"') >= 2, f"{p} 缺双语词条 {k}"
        assert '"cp.voice.bk_hub_fish": "人设克隆声（云端）"' in js, p
        assert '"cp.voice.bk_avatar_clone": "人设克隆声"' in js, p
    for p, js in _both("components/cp-voice.js"):
        assert "_emoLabel" in js, p   # 情绪值经人话映射（没登记的原样显示）


def test_host_consumes_cp_voice_xl_events():
    """宿主三处消费：xl-changed 联动主输入框行 + 发送 translated 分桶埋点 +
    主输入框开关反向刷新右栏（同文档 storage 事件不触发，必须显式调）。"""
    html = (REPO / "src" / "web" / "templates" / "unified_inbox.html"
            ).read_text(encoding="utf-8")
    assert "addEventListener('cp-voice-xl-changed'" in html
    assert "cpv_sent_xl" in html
    assert ".refreshXl" in html
    # P0-V2c：宿主必须提供显式目标语（我的消息 → X）偏好 getter——右栏跟随
    # 显式设置而非只认会话语言（2026-08-30 用户实测：设了英文仍念中文）
    assert "__cpVoiceXlPref" in html


def test_cp_voice_fallback_reason_branching_in_both_trees():
    """P0 2026-08-31（提示风暴复盘）：回落必须带原因分支——语种改道（刻意保护，
    info 蓝条+「改发原文」一键出路）与通道故障（warn 黄条+报障指路）是两种事，
    混成一句「通道中断」当日实测制造无效工单（通道全绿）。同批钉住：
    ① 无声探测=真闸门（禁发+确认双保险）；② 降级发送前显式确认（原「回落
    必须显式」不变量由确认弹层承接且更强）；③ 三条结果提示互斥单出口
    _syncNotes（无声>过期>回落，并列=没有重点）；④ 顶部语种预告与预览区
    语种改道说明同因去重。"""
    for p, js in _both("components/cp-voice.js"):
        assert "fallback_reason" in js, p                       # 服务端原因消费
        assert 'data-act="xl-off-regen"' in js, p               # 一键出路按钮
        assert "cp.voice.fallback_lang" in js, p                # 语种改道文案
        assert "cp.voice.fallback_quota" in js, p               # 额度耗尽文案
        assert "cp.voice.fallback_warn" in js, p                # 通道故障文案（保留）
        assert "cp.voice.fallback_send_confirm" in js, p        # 降级发送确认
        assert "_syncNotes" in js, p                            # 提示互斥单出口
        assert "this._silent" in js, p                          # 无声阻发闸
        assert "cp.voice.silent_block_t" in js, p               # 禁发原因 title
        assert "_warnBeacon" in js, p                           # 警示观测
        assert "_xlOffRegen" in js, p
        # 老后端兜底：缺 fallback_reason 时按「已翻译且目标语超出主路能力」推断
        assert "_langUnsupported(String(d.target_lang" in js, p


def test_cp_voice_fallback_keys_bilingual_in_both_trees():
    keys = (
        "cp.voice.fallback_lang", "cp.voice.fallback_lang_btn",
        "cp.voice.fallback_quota", "cp.voice.fallback_send_confirm",
        "cp.voice.silent_block_t",
    )
    for p, js in _both("i18n/cp-i18n.js"):
        for k in keys:
            # zh/en 双字典各出现一次 → 每键至少 2 次
            assert js.count(f'"{k}"') >= 2, f"{p} 缺双语词条 {k}"


def test_host_consumes_cp_voice_warn():
    """警示观测最后一公里：组件发 cp-voice-warn，宿主必须转 cpv_warn_* 埋点
    ——否则「提示风暴发生率」永远只能靠老板截图发现。"""
    html = (REPO / "src" / "web" / "templates" / "unified_inbox.html"
            ).read_text(encoding="utf-8")
    assert "addEventListener('cp-voice-warn'" in html
    assert "__cpVoiceWarnBound" in html
    assert "cpv_warn_" in html


def test_voice_meta_reason_forwarded_on_server():
    """服务端两条语音路由都必须转发 fallback_reason/fallback_lang（additive；
    语义门禁在 tests/test_voice_fallback_reason.py，这里钉「写了没挂线」）。"""
    for rel in (("src", "web", "routes", "voice_routes.py"),
                ("src", "web", "routes", "unified_inbox_send_routes.py")):
        src_text = REPO.joinpath(*rel).read_text(encoding="utf-8")
        assert '"fallback_reason"' in src_text, rel[-1]
        assert "fallback_reason_from_extra" in src_text, rel[-1]
        # P1 2026-08-31 试听=发送语言路由收口：两路由都必须过
        # route_voice_cfg_for_text（clone_langs 语种→克隆节点改派；行为语义
        # 在 tests/test_voice_tts_fast_preview.py 任务 3 组）。
        assert "route_voice_cfg_for_text" in src_text, rel[-1]


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
