# -*- coding: utf-8 -*-
"""P0-198 出站保真三防线的静态接线门禁（2026-07-31）。

背景（客户机实录）：
  ① 中文『是Steven，别担心。😊』原样发到英文客户手机（identity 跳过）；
  ② 同一入站的三条同义改写全部发出（生成层重生、投递层无核对）；
  ③ WhatsApp 开消息时限的聊天里，出站被官方客户端标灰色 (i)「旧版 WhatsApp」；
  ④ libsignal 把 Signal 会话私钥原文 dump 进客户机日志。

纯静态源码扫描（与 _sidecar_contract / _inline_handler_scan 同哲学）：接线是「写了
函数没挂线」的高发区，行为测试盖不住「忘了在路由里调」这一层。
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]

SEND_ROUTES = REPO / "src" / "web" / "routes" / "unified_inbox_send_routes.py"
SERVER_JS = REPO / "services" / "whatsapp-baileys" / "server.js"
INBOX_TPL = REPO / "src" / "web" / "templates" / "unified_inbox.html"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ── ① 语言错配拦截：路由必须接 CJK 冲突守卫 ─────────────────────────────────

def test_send_route_wires_lang_mismatch_guard():
    src = _read(SEND_ROUTES)
    assert "from src.inbox.outbound_translate import contains_cjk, lang_is_cjk" in src
    assert '"code": "lang_mismatch"' in src
    assert "err.inbox.lang_mismatch" in src
    # 冲突时源语言必须被钉住（绕开混排误检 → identity 跳过）
    assert "_cjk_conflict" in src


def test_send_route_wires_near_duplicate_guard():
    src = _read(SEND_ROUTES)
    assert "near_duplicate_of_recent" in src
    assert '"code": "near_duplicate"' in src
    assert "err.inbox.near_duplicate" in src
    # 幂等占位必须在拦截时释放（否则坐席确认后的重发被自己上一次预留挡住）
    assert src.count("_dedup.release(_dedup_scope, _client_msg_id)") >= 4


def test_frontend_handles_guard_409_with_confirm():
    tpl = _read(INBOX_TPL)
    assert "lang_mismatch" in tpl and "near_duplicate" in tpl
    assert "force_lang" in tpl and "force_dup" in tpl
    assert "inbox.send.force_confirm" in tpl
    # 气泡 chip 默认必须是 opt-in（=== '1' 才开）——交付分级拍板语义
    assert "localStorage.getItem(_BUBBLE_PREF_KEY)==='1'" in tpl


# ── ③ Baileys ephemeral：三条出站路径都要带会话时限 ──────────────────────────

def test_wa_sidecar_sends_carry_ephemeral():
    js = _read(SERVER_JS)
    assert "function rememberEphemeral" in js
    assert "function ephemeralOpts" in js
    assert "function ensureEphemeralKnown" in js
    # 入站学习挂在 pushWaMessage（实时 + 历史回填同一入口）
    assert "rememberEphemeral(entry, jid, msg)" in js
    # 文本 / 媒体 / 编辑 三条出站路径都合并 ephemeralExpiration options
    assert js.count("ephemeralOpts(") >= 3
    assert "ephemeralExpiration" in js
    # extractText 必须先剥 ephemeral 包装（开时限聊天的入站不被当空消息丢弃）
    assert "unwrapWaMessage((msg && msg.message) || {})" in js


# ── ④ 私钥日志脱敏：libsignal SessionEntry dump 不得落盘 ─────────────────────

def test_wa_sidecar_redacts_signal_session_dump():
    js = _read(SERVER_JS)
    assert "Closing session" in js          # 匹配特征
    assert "redacted" in js
    assert "a.currentRatchet || a.indexInfo" in js


# ── ⑤ 错收件人（2026-07-31 12:47 实锤：设备后缀被并进号码）───────────────────

def test_wa_sidecar_strips_device_suffix_both_directions():
    """chat_key '639531765880:0' 曾被 toJid digits 拼成 '6395317658800@s.whatsapp.net'
    —— 主动触达发到不存在的号码，真客户零感知。出站 toJid 与入站镜像都必须
    去设备后缀（与 selfIds 的 split(':')[0] 同口径）。"""
    js = _read(SERVER_JS)
    # toJid：冒号后缀在 digits 拼接**之前**被剥掉
    assert '/^\\d+:\\d+$/.test(s)' in js
    # 入站镜像：remoteJid 归一化后再入库
    assert "function normalizeUserJid" in js
    assert "normalizeUserJid((msg.key && msg.key.remoteJid)" in js


# ── ② HOLD 语义：worker 两条链都必须处理翻译回调的 None ──────────────────────

def test_worker_handles_translate_hold_in_both_paths():
    src = _read(REPO / "src" / "inbox" / "autosend_worker.py")
    assert src.count("translate_hold") >= 2   # 自动链 + 人工通过链各一处
    assert src.count("_tx is None") >= 2
