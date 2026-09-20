# -*- coding: utf-8 -*-
"""P0-V2（2026-08-19）坐席语音「译声」契约：打中文 → 发目标语克隆声。

三层钉住：
  1. ``resolve_spoken_text`` 纯语义（fail-open：翻译异常/失败/空译/identity 一律
     念原文且如实标 translated=False——语音链绝不因翻译层故障断发）；
  2. 路由接线（源码扫描，风格同 test_voice_send_verbatim）：tts-test 与
     send-voice 都先译后念（synthesize(spoken_text)）、复用契约带语言维度、
     收件箱镜像写客户实际听到的话（inbox_text=spoken_text）；
  3. 前端接线：试听/发送都带 target_lang、复用预检带语言、开关函数挂 window。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.ai.voice_outbound_xlate import resolve_spoken_text

_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


class _Res:
    def __init__(self, ok=True, text="", provider="ollama_mt", source_lang="zh"):
        self.ok = ok
        self.translated_text = text
        self.provider = provider
        self.source_lang = source_lang


class _Svc:
    def __init__(self, res=None, boom=False):
        self._res = res
        self._boom = boom
        self.calls = 0

    async def translate(self, text, *, target_lang, style="chat", **kw):
        self.calls += 1
        if self._boom:
            raise RuntimeError("engine down")
        return self._res


# ── 1. 纯语义 ──────────────────────────────────────────────────────────


async def test_translates_and_reports_meta():
    svc = _Svc(_Res(text="こんにちは、ご飯食べた？"))
    spoken, meta = await resolve_spoken_text("你吃饭了吗", "ja", svc)
    assert spoken == "こんにちは、ご飯食べた？"
    assert meta["translated"] is True and meta["target_lang"] == "ja"
    assert meta["provider"] == "ollama_mt"


async def test_fail_open_on_exception():
    spoken, meta = await resolve_spoken_text("你吃饭了吗", "ja", _Svc(boom=True))
    assert spoken == "你吃饭了吗"
    assert meta["translated"] is False and meta["reason"] == "translate_error"


async def test_fail_open_on_not_ok_or_empty():
    s1, m1 = await resolve_spoken_text("你好", "ja", _Svc(_Res(ok=False, text="x")))
    assert s1 == "你好" and m1["reason"] == "translate_failed"
    s2, m2 = await resolve_spoken_text("你好", "ja", _Svc(_Res(text="  ")))
    assert s2 == "你好" and m2["reason"] == "translate_failed"


async def test_identity_not_marked_translated():
    s, m = await resolve_spoken_text("hello", "en", _Svc(_Res(text="hello")))
    assert s == "hello" and m["translated"] is False and m["reason"] == "identity"
    s2, m2 = await resolve_spoken_text(
        "你好", "zh", _Svc(_Res(text="改写", provider="identity")))
    assert s2 == "你好" and m2["translated"] is False


async def test_no_target_or_unknown_skips_service():
    svc = _Svc(_Res(text="x"))
    s, m = await resolve_spoken_text("你好", "", svc)
    assert s == "你好" and m["reason"] == "no_target" and svc.calls == 0
    s2, m2 = await resolve_spoken_text("你好", "unknown", svc)
    assert s2 == "你好" and m2["reason"] == "no_target" and svc.calls == 0


async def test_no_service_and_no_text():
    s, m = await resolve_spoken_text("你好", "ja", None)
    assert s == "你好" and m["reason"] == "no_service"
    s2, m2 = await resolve_spoken_text("   ", "ja", _Svc(_Res(text="x")))
    assert s2 == "" and m2["reason"] == "no_text"


# ── 2. 路由接线（源码扫描）────────────────────────────────────────────


def test_send_voice_wires_spoken_text():
    src = _read("src/web/routes/unified_inbox_send_routes.py")
    seg = src[src.index("async def api_unified_inbox_send_voice"):]
    assert "resolve_spoken_text" in seg, "send-voice 必须先译后念（fail-open 在 helper 内）"
    assert "tts.synthesize(\n                    spoken_text" in seg.replace("\r\n", "\n"), \
        "合成必须吃 spoken_text（译声=念译文）"
    assert "target_lang=_vt_target" in seg, "试听复用校验必须带语言维度"
    assert seg.count("inbox_text=spoken_text") >= 2, \
        "收件箱镜像必须写客户实际听到的话（含旧签名回落分支）"
    assert "route_voice_cfg_for_text(\n                        voice_cfg, spoken_text" \
        in seg.replace("\r\n", "\n"), "音色语言路由必须按 spoken 文本走"


def test_tts_test_wires_spoken_text():
    src = _read("src/web/routes/voice_routes.py")
    seg = src[src.index("async def _run_tts_preview"):]
    assert "resolve_spoken_text" in seg
    assert "tts.synthesize(" in seg and "spoken_text, timeout_sec=" in seg, \
        "试听合成必须吃 spoken_text（试听=发送同稿）"
    assert 'target_lang=(_vt if _xl.get("translated") else "")' in seg, \
        "sidecar 必须按「真的译了」登记语言维度"
    assert '"spoken_text": (spoken_text if _xl.get("translated") else "")' in seg, \
        "sidecar meta 必须携带译稿（发送侧镜像用）"


def test_frontend_wires_target_lang():
    html = _read("src/web/templates/unified_inbox.html")
    gi = html.index("async function genVoiceReply")
    gseg = html[gi:gi + 5000]
    assert "target_lang: _vxl||undefined" in gseg, "试听必须带译声目标语"
    si = html.index("async function sendVoiceReply")
    sseg = html[si:si + 5000]
    assert "target_lang: _voiceXlTarget()||undefined" in sseg, "发送必须带译声目标语"
    assert "_pv.lang" in sseg, "复用预检必须含语言维度（服务端另有硬校验）"
    assert "_onVoiceXlToggle" in html[html.index("Object.assign(window,{replyToMsg"):], \
        "译声开关（内联 onchange）必须挂 window"
