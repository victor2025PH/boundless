# -*- coding: utf-8 -*-
"""AI 草稿逐条发送随带 source_lang（2026-09-18 P1 重录实锤）。

事故链：smart-reply 按 reply_lang 决策把正文写成客户语言（西语）→ 逐条发送 body 只带 target_lang
（会话「发→X」）→ 服务端出站翻译自检源语言，短西语句「Sí, enviamos a Chile en 7 a 10 días.」无关键词
被判 en → 「en→es」把西语「译」成英文「Yes, we ship to Chile in 7 to 10 days.」发给西语客户。

钉住：① 生成后记下草稿正文语言（译文则为目标语）；② 逐条发送在 target_lang 存在且正文无 CJK 时
带 source_lang（服务端 target==source 直通 / 异语种方向正确）；③ 含 CJK 正文不带（服务端 CJK 护栏钉 zh）；
④ 服务端 send 路由的同语种直通条件仍在（source_lang 参与 identity 判定）。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_HTML = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_SEND = _ROOT / "src" / "web" / "routes" / "unified_inbox_send_routes.py"


def _fn(src: str, header: str) -> str:
    i = src.index(header)
    j = src.find("\n}\n", i)
    return src[i:j]


def test_generate_records_draft_text_lang():
    src = _HTML.read_text(encoding="utf-8")
    assert "var _dpickTextLang=''" in src
    gen = _fn(src, "async function _dpickGenerate(")
    assert "var text=d.translated||d.reply;" in gen
    # 译文用目标语（坐席手选 lang），否则用服务端 reply_lang 决策
    assert re.search(r"_dpickTextLang=d\.translated\?String\(lang\|\|''\):String\(d\.reply_lang\|\|''\)", gen)


def test_send_parts_pass_source_lang_only_for_non_cjk_with_target():
    src = _HTML.read_text(encoding="utf-8")
    send = _fn(src, "async function _sendDraftParts(")
    assert "if(_xlateOut) body.target_lang=_xlateOut;" in send
    m = re.search(r"if\(body\.target_lang && _dpickTextLang && !/\[\\u3400-\\u9fff\]/\.test\(text\)\) body\.source_lang=_dpickTextLang;", send)
    assert m, "逐条发送未按条件带 source_lang"
    # 顺序：先定 target_lang，再据此带 source_lang
    assert send.index("body.target_lang=_xlateOut") < m.start()


def test_server_send_identity_skip_uses_source_lang():
    src = _SEND.read_text(encoding="utf-8")
    # 目标语 == 调用方给的源语 → 不译（同语种直通）；这是前端带 source_lang 的服务端落点
    assert 'target_lang.lower() not in ("unknown", source_lang.lower())' in src
    assert "source_lang = normalize_lang(source_lang)" in src
