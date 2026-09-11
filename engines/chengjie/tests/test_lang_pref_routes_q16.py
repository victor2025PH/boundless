# -*- coding: utf-8 -*-
"""#276 Q-16（2026-09-11）「发→X」按会话 + 手发确认 —— 路由日志 / 前端回归门禁。

事故：A 会话选「发→ja」后切到 B（English 客户），中文手发被译成日语发出。
根因：前端 ``_persistXlatePrefs`` 把含 ``out`` 的整份 entry 写进 ``__default__``，
``_loadXlateForConv`` 切会话又拿 ``bd.out`` 当初值——「发→X」跨会话继承。

本门禁钉四条：
1. POST /conv-xlate-out 每次真实落库留 ``[lang_pref] conv=… scope=conv send=<lang|clear> by=<source>``；
   空串 no-op 不留行（没落库就没日志，别制造假审计）。
2. 前端 ``__default__`` 不再含 ``out``；切会话初值 ``convEntry.out || ''``，不碰 ``bd.out``；
   存量 ``__default__.out`` 读到即丢。
3. node 真跑前端函数（源码抽取 + 最小 DOM/localStorage 桩）：会话 A 设 ja → 切无设置的
   会话 B → ``_xlateOut===''``，手发请求体**无** ``target_lang``。
4. ``_xlMismatchCheck`` 真值表：仅 目标语≠客户语言 才要确认；''/auto/未知 恒不弹。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes

_CONV = {"platform": "telegram", "account_id": "7331682688", "chat_key": "8852939166"}
_CID = "telegram:7331682688:8852939166"
_ROOT = Path(__file__).resolve().parent.parent
_INBOX_HTML = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_LOGGER = "src.web.routes.unified_inbox_translate_routes"


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


def _client(tmp_path):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(app, page_auth=page_auth, api_auth=api_auth, templates=_Templates())
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    return TestClient(app)


def _post(c, **extra):
    return c.post("/api/unified-inbox/conv-xlate-out", json=dict(_CONV, **extra)).json()


def _pref_lines(caplog):
    return [r.getMessage() for r in caplog.records if "[lang_pref]" in r.getMessage()]


# ── B：路由日志 ─────────────────────────────────────────────────────────────

def test_set_logs_lang_pref_with_scope_and_source(tmp_path, caplog):
    c = _client(tmp_path)
    with caplog.at_level("INFO", logger=_LOGGER):
        assert _post(c, lang="ja", source="ui_select")["lang"] == "ja"
    lines = _pref_lines(caplog)
    assert lines == [f"[lang_pref] conv={_CID} scope=conv send=ja by=ui_select"], lines


def test_auto_logs_send_auto(tmp_path, caplog):
    c = _client(tmp_path)
    with caplog.at_level("INFO", logger=_LOGGER):
        _post(c, lang="auto", source="ui_select")
    assert _pref_lines(caplog) == [f"[lang_pref] conv={_CID} scope=conv send=auto by=ui_select"]


def test_clear_logs_send_clear_by_clear(tmp_path, caplog):
    """清除语义：``send=clear by=clear``（clear:true 覆盖 body 里残留的 lang/source）。"""
    c = _client(tmp_path)
    _post(c, lang="ja", source="ui_select")
    with caplog.at_level("INFO", logger=_LOGGER):
        d = _post(c, lang="ja", clear=True, source="ui_select")
    assert d["ok"] is True and d["lang"] == ""
    assert _pref_lines(caplog) == [f"[lang_pref] conv={_CID} scope=conv send=clear by=clear"]


def test_empty_noop_does_not_log(tmp_path, caplog):
    """空串 no-op 没落库 → 不留 [lang_pref]（假审计比没审计更糟）。"""
    c = _client(tmp_path)
    _post(c, lang="ja", source="ui_select")
    with caplog.at_level("INFO", logger=_LOGGER):
        d = _post(c, lang="")
    assert d["noop"] is True
    assert _pref_lines(caplog) == []


def test_unknown_source_defaults_to_api(tmp_path, caplog):
    c = _client(tmp_path)
    with caplog.at_level("INFO", logger=_LOGGER):
        _post(c, lang="en")
    assert _pref_lines(caplog) == [f"[lang_pref] conv={_CID} scope=conv send=en by=api"]


def test_bad_lang_does_not_log(tmp_path, caplog):
    c = _client(tmp_path)
    with caplog.at_level("INFO", logger=_LOGGER):
        assert _post(c, lang="tlh")["error"] == "bad_lang"
    assert _pref_lines(caplog) == []


# ── A：前端源码钉 ────────────────────────────────────────────────────────────

def _src() -> str:
    return _INBOX_HTML.read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    return src.split(start, 1)[1].split(end, 1)[0]


def test_frontend_default_entry_has_no_out():
    body = _between(_src(), "function _persistXlatePrefs()", "function _syncConvXlateOutSrv")
    m = re.search(r"prefs\.__default__=(\{[^}]*\});", body)
    assert m, "找不到 prefs.__default__ 赋值"
    keys = re.findall(r"(\w+):", m.group(1))
    assert "out" not in keys, f"__default__ 又带上 out 了：{m.group(1)}"
    assert set(keys) == {"in", "preview", "back", "tone"}, keys
    assert "prefs.__default__=entry" not in body


def test_frontend_conv_switch_does_not_inherit_bd_out():
    body = _between(_src(), "function _loadXlateForConv(c)", "\nasync function _loadServerDefaultLang")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", body, flags=re.S))   # 去注释后看真代码
    assert "_xlateOut=(convEntry&&convEntry.out)||'';" in body
    assert "bd.out" not in code, "切会话初值又在继承浏览器 __default__.out"
    assert "_hydrateConvXlateOut(c)" in body, "服务端事实源 hydrate 不能丢（#154）"


def test_frontend_migration_drops_legacy_default_out():
    body = _between(_src(), "function _xlatePrefs()", "function _sameLang")
    assert "delete xlDef.out" in body
    assert "localStorage.setItem(_XL_KEY" in body


def test_frontend_send_paths_carry_confirm_and_guarded_target_lang():
    src = _src()
    send = _between(src, "async function sendMsg()", "\nasync function _sendDraftParts")
    assert "_xlConfirmMismatch(_outLang)" in send
    assert "if(_serverXlate) body.target_lang=_outLang;" in send
    assert "if(_xlConfirmed) body.xl_confirmed=1;" in send
    parts = src.split("async function _sendDraftParts(parts)", 1)[1][:6000]
    assert "_xlConfirmMismatch(_xlateOut)" in parts
    assert "if(_xlateOut) body.target_lang=_xlateOut;" in parts
    assert "if(_xlBatchConfirmed) body.xl_confirmed=1;" in parts


def test_i18n_keys_exist_in_all_three_langs():
    from src.web.web_i18n import get_translations
    keys = ("inbox.xl.out_auto_peer", "inbox.xl.out_auto_peer_unk", "inbox.xl.out_auto_peer_sub",
            "inbox.xl.dir_scope_conv", "inbox.xl.dir_scope_t", "inbox.xl.confirm_mismatch")
    zh, en, hant = get_translations("zh"), get_translations("en"), get_translations("zh_hant")
    for k in keys:
        assert zh.get(k) and en.get(k) and hant.get(k), k
        assert hant[k] != zh[k], f"zh_hant 未转正：{k}"
    assert "{to}" in zh["inbox.xl.confirm_mismatch"] and "{cust}" in zh["inbox.xl.confirm_mismatch"]


# ── F：node 真跑前端函数（源码抽取 + 最小桩） ─────────────────────────────────

def _extract_fn(src: str, name: str) -> str:
    """从模板抽出 ``function NAME(...){...}`` 全文（括号配对，跳过字串/注释）。"""
    i = src.index(f"function {name}(")
    j = src.index("{", i)
    depth, k, n = 0, j, len(src)
    while k < n:
        ch = src[k]
        if ch in "'\"`":
            q = ch
            k += 1
            while k < n and src[k] != q:
                k += 2 if src[k] == "\\" else 1
        elif src.startswith("//", k):
            k = src.index("\n", k)
        elif src.startswith("/*", k):
            k = src.index("*/", k) + 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"unbalanced braces in {name}")


_HARNESS_HEAD = r"""
const _store={};
const localStorage={getItem:k=>(k in _store)?_store[k]:null,setItem:(k,v)=>{_store[k]=String(v);},removeItem:k=>{delete _store[k];}};
const _els={};
const document={getElementById:id=>(_els[id]=_els[id]||{value:'',checked:false,classList:{toggle(){},add(){},remove(){},contains(){return false;}},style:{}})};
const window={T:k=>k,Tf:(k,p)=>k+JSON.stringify(p||{}),confirm:()=>true};
const _XL_SET={zh:1,en:1,ja:1,ko:1,th:1,vi:1,id:1,ru:1,es:1,pt:1,fr:1,de:1,tl:1,ms:1,yue:1,'zh-tw':1};
let selectedChat=null, threadMsgs=[];
function convKey(c){ return c.platform+':'+c.account_id+':'+c.chat_key; }
function _agentLang(){ return 'zh'; }
function _convLooksYue(){ return false; }
function _syncToneSeg(){} function _resetOutPreview(){} function _loadConvPrefEngine(){}
function _loadServerDefaultLang(){} function _hydrateConvXlateOut(){} function _updateEngineHint(){}
function _updateXlStatus(){} function _syncTopXlate(){} function _syncLangWarn(){} function _syncVoiceXlRow(){}
function _uiBeacon(){} function _xlLangName(c){ return String(c||''); }
let _convPrefEngine='', _xlServerDefaultApplicable=false;
"""

_HARNESS_TAIL = r"""
const A={platform:'telegram',account_id:'1',chat_key:'A',language:'ja'};
const B={platform:'telegram',account_id:'1',chat_key:'B',language:'en'};
const out={};
// 存量迁移：老浏览器 __default__.out=ja 读到即丢，其它字段保留
localStorage.setItem(_XL_KEY, JSON.stringify({__default__:{in:'zh',out:'ja',preview:false,back:true,tone:'chat'}}));
const migrated=_xlatePrefs();
out.migrated_default=migrated.__default__;
out.migrated_persisted=JSON.parse(localStorage.getItem(_XL_KEY)).__default__;
// 会话 A：坐席选「发→ja」
selectedChat=A; _loadXlateForConv(A);
_xlateOut='ja'; _persistXlatePrefs();
out.default_after_A=_xlatePrefs().__default__;
out.A_entry_out=_xlatePrefs()[convKey(A)].out;
// 切到无设置的会话 B
selectedChat=B; _loadXlateForConv(B);
out.B_xlateOut=_xlateOut;
const body={platform:B.platform,account_id:B.account_id,chat_key:B.chat_key,text:'你好'};
if(_xlateOut) body.target_lang=_xlateOut;        // 与 sendMsg/_sendDraftParts 同一守卫
out.B_body_has_target=Object.prototype.hasOwnProperty.call(body,'target_lang');
// 回到 A：会话级条目仍在
selectedChat=A; _loadXlateForConv(A);
out.A_back_xlateOut=_xlateOut;
// 「收→」仍走全局默认（B 改 in=en 后新会话 C 继承 in，不继承 out）
selectedChat=B; _loadXlateForConv(B); _xlateIn='en'; _xlateOut='ko'; _persistXlatePrefs();
const C={platform:'telegram',account_id:'1',chat_key:'C',language:'th'};
selectedChat=C; _loadXlateForConv(C);
out.C_in=_xlateIn; out.C_out=_xlateOut;
// 手发确认真值表
out.mm=[
  _xlMismatchCheck('ja','en'), _xlMismatchCheck('auto','en'), _xlMismatchCheck('','en'),
  _xlMismatchCheck('en','en'), _xlMismatchCheck('ja','unknown'), _xlMismatchCheck('ja',''),
  _xlMismatchCheck('zh-tw','zh'), _xlMismatchCheck('en','en-US'),
];
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用：跳过前端函数真跑门禁")
def test_node_conv_switch_does_not_carry_send_lang(tmp_path):
    src = _src()
    consts = _between(src, "/* ===== P55: 实时双向翻译 ===== */", "function _xlatePrefs()")
    fns = [_extract_fn(src, n) for n in (
        "_xlatePrefs", "_sameLang", "_canonXlLang", "_loadXlateForConv",
        "_persistXlatePrefs", "_xlMismatchCheck")]
    js = _HARNESS_HEAD + consts + "\n" + "\n".join(fns) + _HARNESS_TAIL
    p = Path(tempfile.mkdtemp()) / "q16_harness.js"
    p.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(p)], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr[-1500:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    # 存量迁移
    assert "out" not in out["migrated_default"] and out["migrated_default"]["in"] == "zh"
    assert "out" not in out["migrated_persisted"]
    # A 设 ja → __default__ 仍无 out；会话级条目有
    assert "out" not in out["default_after_A"], out["default_after_A"]
    assert out["A_entry_out"] == "ja"
    # 切 B：不继承；手发请求体无 target_lang
    assert out["B_xlateOut"] == ""
    assert out["B_body_has_target"] is False
    # 回 A：会话级仍在
    assert out["A_back_xlateOut"] == "ja"
    # 「收→」全局默认语义保留（in 继承），「发→」不继承
    assert out["C_in"] == "en" and out["C_out"] == ""
    # 确认真值表：仅 目标语≠客户语言 才要确认
    assert out["mm"] == ["en", "", "", "", "", "", "zh", ""], out["mm"]
