# -*- coding: utf-8 -*-
"""#154 上午形态（2026-09-04 I-2 尾巴 d）：坐席侧出站 ✎ 原文副行语言锁定会话「收→」。

实录（JV7MGQ 包）：会话「收→中·发→英」，客户发日语 → LLM 草稿日语 → sendpoint 守卫按
pin=en 译成英文真发英文（客户侧正确），但 ``outbound_translations.original_text`` 记的是
**日语草稿**，✎ 行把它当「坐席原文」渲染，且 ``_outHasOrig`` 把它当「已双语」压掉了
中文补译 → 坐席看着像「AI 用日语回了客户」。

服务端不知道会话「收→」（那是坐席端逐会话状态），锁定只能落在前端：
``_origReadable(meta)`` —— 记录的 ``source_lang`` 与 ``_xlateIn`` 不同语 → 不算对照原文，
既不渲染 ✎ 行，也不抑制按「收→」补译。两个消费点（渲染 / 补译判定）必须同一判据。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_HTML = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_AGG = _ROOT / "src" / "web" / "routes" / "unified_inbox_aggregate.py"
_OUTXL = _ROOT / "src" / "inbox" / "outbound_translate.py"


def _html() -> str:
    return _HTML.read_text(encoding="utf-8")


def _fn_src(html: str, name: str) -> str:
    """抠出顶层 ``function name(...){...}`` 源码（花括号配对，函数体内无字符串花括号即可）。"""
    m = re.search(r"^function " + re.escape(name) + r"\(", html, re.M)
    assert m, f"unified_inbox.html 丢失 function {name}"
    i = html.index("{", m.end())
    depth, j = 0, i
    while j < len(html):
        c = html[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[m.start():j + 1]
        j += 1
    raise AssertionError(f"{name} 花括号不配对")


def test_orig_readable_helper_defined_once_top_level():
    html = _html()
    hits = re.findall(r"^function _origReadable\(meta\)", html, re.M)
    assert len(hits) == 1, "_origReadable 必须且只能有一个顶层定义（渲染与补译两处共用）"
    src = _fn_src(html, "_origReadable")
    assert "_xlateIn" in src and "_sameLang(" in src, "_origReadable 判据必须是 source_lang × 会话「收→」(_xlateIn) 同语比对"
    assert "source_lang" in src


def test_out_has_orig_consults_readability():
    """补译判定：原文不可读 → 不得再当「已双语」压掉按「收→」的补译。"""
    src = _fn_src(_html(), "_outHasOrig")
    assert src.count("_origReadable(") >= 2, (
        "_outHasOrig 的服务端 agent_original 与会话内存 _outOrigMap 两条分支都必须过 _origReadable"
    )
    assert "return true;" not in src.replace("return _origReadable", ""), (
        "_outHasOrig 不得再对「有原文」无条件 return true（那正是日语草稿压掉中文补译的路径）"
    )


def test_render_branch_consults_readability():
    """渲染：✎ 行两条来源（agent_original / _outOrigMap）都必须过同一判据。"""
    html = _html()
    assert re.search(
        r"m\.agent_original && _meaningfulXlate\(m\.text,m\.agent_original\) && _origReadable\(m\.agent_xlate\|\|null\)",
        html,
    ), "渲染分支 agent_original 未过 _origReadable"
    assert re.search(
        r"_o\.orig && _meaningfulXlate\(m\.text,_o\.orig\) && _origReadable\(_o\.meta\|\|null\)",
        html,
    ), "渲染分支 _outOrigMap 未过 _origReadable"


def test_server_side_carries_source_lang_evidence():
    """判据的证据链：aggregate 富集必须把 source_lang 挂进 agent_xlate；自动链写入必须记 source。"""
    agg = _AGG.read_text(encoding="utf-8")
    seg = agg[agg.index("def _enrich_outbound_originals"):]
    seg = seg[: seg.find("\ndef ", 10) if seg.find("\ndef ", 10) > 0 else len(seg)]
    assert '"source_lang": row.get("source_lang")' in seg, "aggregate agent_xlate 丢失 source_lang（前端失去判据）"
    outxl = _OUTXL.read_text(encoding="utf-8")
    assert re.search(r"record_outbound_translation\(\s*cid, translated, text,\s*source_lang=eff_source", outxl), (
        "outbound_translate 自动链记录丢失 source_lang=eff_source（日语草稿将无法被识别为不可读）"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑前端纯函数真值表")
def test_orig_readable_truth_table_in_node():
    """把 _canonXlLang / _sameLang / _origReadable 三个纯函数抠出来在 node 里跑金标。"""
    html = _html()
    js = "\n".join([
        "var _XL_SET={ja:1,en:1,zh:1,ko:1,th:1,vi:1,es:1,fr:1,ru:1,id:1,tl:1,yue:1,'zh-tw':1};",
        "var _xlateIn='';",
        _fn_src(html, "_canonXlLang"),
        _fn_src(html, "_sameLang"),
        _fn_src(html, "_origReadable"),
        """
        var out={};
        _xlateIn='zh';
        out.ja_vs_zh = _origReadable({source_lang:'ja'});        // 实录：日语草稿 × 收→中 → 不可读
        out.zh_vs_zh = _origReadable({source_lang:'zh'});        // 坐席手打中文 → 可读
        out.zhcn_vs_zh = _origReadable({source_lang:'zh-CN'});   // 变体归一：zh-CN=zh
        out.zhtw_vs_zh = _origReadable({source_lang:'zh-tw'});   // 繁中不折叠回 zh（与后端三处联动口径一致）
        out.unknown_src = _origReadable({source_lang:''});       // 未知源语 → 保持旧行为（可读）
        out.null_meta = _origReadable(null);
        _xlateIn='';
        out.no_in_setting = _origReadable({source_lang:'ja'});   // 未设「收→」 → 旧行为
        _xlateIn='en';
        out.en_vs_en = _origReadable({source_lang:'en'});
        out.zh_vs_en = _origReadable({source_lang:'zh'});        // 英文坐席看中文原文 → 不可读 → 走补译
        console.log(JSON.stringify(out));
        """,
    ])
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {
        "ja_vs_zh": False,
        "zh_vs_zh": True,
        "zhcn_vs_zh": True,
        "zhtw_vs_zh": False,
        "unknown_src": True,
        "null_meta": True,
        "no_in_setting": True,
        "en_vs_en": True,
        "zh_vs_en": False,
    }, out
