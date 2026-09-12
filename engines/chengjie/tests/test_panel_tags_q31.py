# -*- coding: utf-8 -*-
"""Q-31 #317（BS7E5Q）：筛选面板系统标签筹码——处理完自动消 / 可关闭 / 单人端默认折叠。

四例（指令 C 段）：
1. 单坐席渲染：系统族（dormant:* / risk:* / 停联 / 接管）折进「系统标签 ▸ N」且默认收起（多坐席展开）；
   用户标签照旧 Top8、不受影响。
2. dormant 会话来新消息后 ``dormant:ignored`` 不在 conv_tags（唯一挂点 ingest_incoming；历史重放 / 回填不摘）。
3. 停联确认后「客户要求停联」不在 conv_tags（事实进 conv_meta.stop_contact_at + 归档），
   且 stop_contact 守卫仍拦第二条（frozen_reason 读 meta，告别「最多一条」不弱化）；人工解冻才清。
4. chip 尾部 × / Esc ＝ 清筛选（setTagFilter('')），不删标签（不碰 _removeConvTag / 标签库）。
前端两例用 node 抽模板函数真跑（缺 node → skip 不假绿），其余真库。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import Request   # 模块级：本文件开了 PEP 563，Depends 依赖函数的标注要能在模块 globals 解析

from src.ai.chat_assistant_service import quick_risk
from src.inbox import dormant_review as dr
from src.inbox import stop_contact as sc
from src.inbox.drafts import DraftService
from src.inbox.store import InboxStore
from src.integrations.protocol_autoreply import HANDOFF_TAG
from src.integrations.protocol_bridge import ingest_incoming

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "unified_inbox.html"
PLAT, ACCT = "whatsapp", "17345893728"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_AUTOSEND_SHADOW_DIR", str(tmp_path / "shadow"))
    from src.inbox import autosend_policy as pol
    monkeypatch.delenv(pol.ENV_POLICY_MODE, raising=False)
    monkeypatch.setattr(pol, "current_policy_mode", lambda: pol.POLICY_SHADOW)
    from src.inbox import account_blocklist as ab
    ab.reset_for_tests()
    dr.reset_for_tests()
    yield
    ab.reset_for_tests()
    dr.reset_for_tests()


@pytest.fixture
def store(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    yield s
    s.close()


def _conv(ck: str, name: str = "Sinue"):
    return {"conversation_id": f"{PLAT}:{ACCT}:{ck}", "platform": PLAT, "account_id": ACCT,
            "chat_key": ck, "display_name": name}


def _push(store, ck, text, ts, mid, direction="in", backfill=False, name="Sinue"):
    return ingest_incoming(store, platform=PLAT, account_id=ACCT, chat_key=ck, name=name,
                           text=text, ts=ts, msg_id=mid, direction=direction,
                           backfill=backfill, backfill_source="history_set" if backfill else "")


# ═══════════════ 前端：模板函数抽取 + node 真跑 ═══════════════

def _src() -> str:
    return _TPL.read_text(encoding="utf-8")


def _extract_fn(src: str, name: str) -> str:
    """抽 ``function NAME(...){...}`` 全文（括号配对，跳过字串 / 注释 / 模板字面量）。"""
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


def _between(src: str, a: str, b: str) -> str:
    i = src.index(a)
    j = src.index(b, i + len(a))
    return src[i:j]


_HARNESS_HEAD = r"""
const _store={};
const localStorage={getItem:k=>(k in _store)?_store[k]:null,setItem:(k,v)=>{_store[k]=String(v);}};
const _els={'fp-tags':{hidden:false,innerHTML:''},'fp-sec-tags':{hidden:false},'ftab-more-menu':{classList:{contains:c=>_menuShow}}};
let _menuShow=false;
const document={getElementById:id=>_els[id]||null,addEventListener(){}};
const window={T:k=>k,Tf:(k,p)=>k};
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
var _NEEDS_HUMAN_TAG='\u9700\u4eba\u5de5';
var _RK_MEDIUM_TAG='risk:medium';
const calls=[];
function _uiBeacon(k){ calls.push(['beacon',k]); }
function setTagFilter(t){ calls.push(['setTagFilter',t]); tagFilter=t; }
function _removeConvTag(t){ calls.push(['_removeConvTag',t]); }
function _saveConvTags(t){ calls.push(['_saveConvTags',t]); }
function _closeFtabMore(){ calls.push(['_closeFtabMore']); }
let tagFilter='';
let _tagStatsCache=[];
var MULTI_SEAT=false;
"""

_STATS = ("_tagStatsCache=[{tag:'dormant:ignored',count:6},{tag:'risk:medium',count:3},"
          "{tag:'\\u5ba2\\u6237\\u8981\\u6c42\\u505c\\u8054',count:1},{tag:'VIP',count:4},"
          "{tag:'\\u9700\\u4eba\\u5de5',count:9},{tag:'\\u4eba\\u5de5\\u63a5\\u7ba1\\u4e2d',count:2},"
          + ",".join("{tag:'t%d',count:1}" % i for i in range(1, 9)) + "];\n")


def _node_run(js: str, tmp_path) -> dict:
    p = tmp_path / "q31_harness.js"
    p.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(p)], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr[-1500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _frontend_js(src: str) -> str:
    tk_re = _between(src, "var _TK_RE=[", "function _tagKind(")
    stop_tag = _between(src, "var _STOP_CONTACT_TAG=", "\n")
    fns = [_extract_fn(src, n) for n in (
        "_tagKind", "_sysTagLabel", "_isSysTag", "_fpSysOpen", "_fpSysToggle", "_renderPanelTags")]
    return _HARNESS_HEAD + tk_re + "\n" + stop_tag + "\n" + "\n".join(fns) + "\n"


_NEEDS_NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用：跳过前端函数真跑门禁")


@_NEEDS_NODE
def test_single_seat_folds_system_tags_user_tags_top8_untouched(tmp_path):
    """例 1：单坐席 → 系统族折进「系统标签 ▸ N」且收起；多坐席展开；用户标签照旧 Top8。"""
    src = _src()
    tail = _STATS + r"""
const out={};
_renderPanelTags(); out.single=_els['fp-tags'].innerHTML;
MULTI_SEAT=true; _renderPanelTags(); out.multi=_els['fp-tags'].innerHTML;
MULTI_SEAT=false; tagFilter='risk:medium'; _renderPanelTags(); out.active_sys=_els['fp-tags'].innerHTML;
out.isSys=['dormant:ignored','risk:medium','risk:high','\u5ba2\u6237\u8981\u6c42\u505c\u8054','\u4eba\u5de5\u63a5\u7ba1\u4e2d','\u9700\u4eba\u5de5','VIP','dormant','risky','\u6210\u4ea4'].map(_isSysTag);
console.log(JSON.stringify(out));
"""
    out = _node_run(_frontend_js(src) + tail, tmp_path)
    single = out["single"]
    # 系统族判定：三族前缀 / 停联 / 接管 / 需人工为真；用户标签、形似前缀（dormant / risky）、成交为假
    assert out["isSys"] == [True, True, True, True, True, True, False, False, False, False], out["isSys"]
    # 折叠头在场、单坐席默认收起（aria-expanded=false + body hidden）
    assert 'class="fp-sys-hd"' in single and 'aria-expanded="false"' in single
    assert '<div class="fp-sys-body" hidden>' in single
    head, body = single.split('<div class="fp-sys-body"', 1)
    # 用户列：VIP + t1..t7 恰 8 个（Top8），系统族一个不漏进去
    user_chips = [m for m in head.split("<button") if 'data-ptag="' in m]
    assert len(user_chips) == 8, len(user_chips)
    assert all(not any(k in m for k in ("dormant:", "risk:", "\u5ba2\u6237\u8981\u6c42\u505c\u8054",
                                         "\u4eba\u5de5\u63a5\u7ba1\u4e2d")) for m in user_chips)
    assert 'data-ptag="VIP"' in head and 'data-ptag="t7"' in head and 'data-ptag="t8"' not in single
    # 系统列：四枚（dormant:ignored / risk:medium / 停联 / 接管），「需人工」照旧剔除（主行一等公民 chip）
    for t in ("dormant:ignored", "risk:medium", "\u5ba2\u6237\u8981\u6c42\u505c\u8054",
              "\u4eba\u5de5\u63a5\u7ba1\u4e2d"):
        assert f'data-ptag="{t}"' in body, t
    assert 'data-ptag="\u9700\u4eba\u5de5"' not in single
    assert '<span class="t-cnt">4</span></button><div class="fp-sys-body"' in single   # 头上 N=4
    # 多坐席默认展开
    assert 'aria-expanded="true"' in out["multi"] and '<div class="fp-sys-body">' in out["multi"]
    # 正按系统标签筛 → 单坐席也强制展开，激活 chip 可见且带 ×
    assert 'aria-expanded="true"' in out["active_sys"]
    assert 'class="fp-chip active" data-ptag="risk:medium"' in out["active_sys"]
    assert out["active_sys"].count('data-ptag-x="1"') == 1


@_NEEDS_NODE
def test_chip_x_and_esc_clear_filter_not_tags(tmp_path):
    """例 4：× 只长在激活 chip 上；点 × / 面板开着按 Esc ＝ setTagFilter('')，不删标签。"""
    src = _src()
    # 抽两支事件处理器本体（容器级点击委托 + document keydown），当纯函数跑
    click_body = _between(src, "box.addEventListener('click', function(e){",
                          "    // Q-31 #317：筛选面板开着时按 Esc")
    click_body = click_body[len("box.addEventListener('click', function(e){"):]
    click_body = click_body[:click_body.rstrip().rfind("});")]
    key_body = _between(src, "document.addEventListener('keydown', function(e){\n        if(e.key!=='Escape'",
                        "      });\n    }\n  }\n  if(document.readyState==='loading')")
    key_body = key_body[len("document.addEventListener('keydown', function(e){"):]
    tail = _STATS + "const _onClick=function(e){" + click_body + "};\n" \
        + "const _onKey=function(e){" + key_body + "};\n" + r"""
const out={};
tagFilter='VIP'; _renderPanelTags(); out.active_user=_els['fp-tags'].innerHTML;
// 点 ×
calls.length=0; _onClick({target:{closest:s=>s==='[data-ptag-x]'?{}:null}}); out.x_calls=calls.slice();
// 点折叠头 → 只开合，不碰筛选
calls.length=0; tagFilter='VIP'; _onClick({target:{closest:s=>s==='[data-sys-toggle]'?{}:null}}); out.hd_calls=calls.slice(); out.ls=_store['ws_fp_sys_open'];
// 点普通 chip → 切筛选（原语义）
calls.length=0; tagFilter=''; _onClick({target:{closest:s=>s==='[data-ptag]'?{getAttribute:()=>'VIP'}:null}}); out.chip_calls=calls.slice();
// Esc：面板关着 → 不管；开着且在筛 → 清筛选；开着没在筛 → 关面板
calls.length=0; tagFilter='VIP'; _menuShow=false; _onKey({key:'Escape',preventDefault(){}}); out.esc_closed=calls.slice();
calls.length=0; tagFilter='VIP'; _menuShow=true; _onKey({key:'Escape',preventDefault(){}}); out.esc_filter=calls.slice();
calls.length=0; tagFilter=''; _menuShow=true; _onKey({key:'Escape',preventDefault(){}}); out.esc_nofilter=calls.slice();
calls.length=0; tagFilter='VIP'; _menuShow=true; _onKey({key:'Enter',preventDefault(){}}); out.enter=calls.slice();
console.log(JSON.stringify(out));
"""
    out = _node_run(_frontend_js(src) + tail, tmp_path)
    # × 只在激活 chip（VIP）尾部，一枚
    assert out["active_user"].count('data-ptag-x="1"') == 1
    assert 'class="fp-chip active" data-ptag="VIP">VIP<span class="t-cnt">4</span><span class="fp-x"' in out["active_user"]
    assert 'aria-label="inbox.filter.chip_clear"' in out["active_user"]
    # × → setTagFilter('') 且**没有**任何删标签调用
    assert ["setTagFilter", ""] in out["x_calls"], out["x_calls"]
    assert not [c for c in out["x_calls"] if c[0] in ("_removeConvTag", "_saveConvTags")], out["x_calls"]
    # 折叠头 → 只记 localStorage + 重绘，不动筛选
    assert not [c for c in out["hd_calls"] if c[0] == "setTagFilter"], out["hd_calls"]
    assert out["ls"] in ("0", "1")
    # 普通 chip 原语义保留
    assert ["setTagFilter", "VIP"] in out["chip_calls"]
    # Esc 三态
    assert out["esc_closed"] == []
    assert ["setTagFilter", ""] in out["esc_filter"] and not [c for c in out["esc_filter"] if c[0] == "_closeFtabMore"]
    assert ["_closeFtabMore"] in out["esc_nofilter"] and not [c for c in out["esc_nofilter"] if c[0] == "setTagFilter"]
    assert out["enter"] == []
    # 源码钉：面板段落里不许出现删标签 / 标签库删除路径（×＝清筛选，不是删标签）
    seg = _between(src, "function _isSysTag(", "function _setFtabCount(")
    for bad in ("_removeConvTag", "_saveConvTags", "tag-library", "method:'DELETE'"):
        assert bad not in seg, bad


def test_panel_tags_does_not_touch_tag_kind_palette_and_i18n_keys_exist():
    """A 段红线：不动 _TK_RE（列表 chip 配色零变化，不新增颜色）；新键 zh / en 齐。"""
    src = _src()
    tk = _between(src, "var _TK_RE=[", "];")
    assert "dormant" not in tk and "risk:" not in tk and "\\u505c\\u8054" not in tk
    from src.web.web_i18n import get_translations
    zh, en = get_translations("zh"), get_translations("en")
    for k in ("inbox.filter.sec_sys_tags", "inbox.filter.sec_sys_tags_t", "inbox.filter.chip_clear",
              "inbox.handoff.bar_ack_stop", "inbox.handoff.bar_ack_stop_t", "inbox.handoff.stop_bar",
              "inbox.handoff.stop_confirm_title", "inbox.handoff.stop_confirm_msg",
              "inbox.handoff.stop_confirm_ok", "inbox.handoff.stop_confirm_done",
              "inbox.handoff.stop_confirm_fail"):
        assert zh.get(k) and en.get(k), k
    # 确认框文案写清后果：归档并从筛选移除 · 不再自动回复
    assert "归档并从筛选移除" in zh["inbox.handoff.stop_confirm_msg"]
    assert "不再自动回复" in zh["inbox.handoff.stop_confirm_msg"]
    # 前端确认框 → 新端点；× / 折叠 / Esc 无内联 onclick（容器委托）
    assert "/stop-contact/confirm" in src
    assert "_appConfirm(window.T('inbox.handoff.stop_confirm_msg')" in src
    assert "data-ptag-x" in src and "data-sys-toggle" in src
    seg = _between(src, "function _renderPanelTags(", "function _setFtabCount(")
    assert "onclick=" not in seg


# ═══════════════ 例 2：dormant:ignored 新入站自动摘 ═══════════════

def test_dormant_ignored_cleared_on_new_real_inbound(store, caplog):
    ck = "447349041791"
    cid = _conv(ck)["conversation_id"]
    old_ts = time.time() - 5 * 86400
    _push(store, ck, "hello are you there", old_ts, "m1", name="Ben")
    dr.record_dormant(store, _conv(ck, "Ben"), text="hello are you there", inbound_ts=old_ts,
                      age_h=120, reason=dr.REASON_STALE)
    r = dr.apply_action(store, cid, "ignore", actor="agent")
    assert r["ok"] and dr.IGNORED_TAG in store.get_conv_tags(cid)
    decided = float((dr.get_dormant_store(store).get(cid) or {}).get("decided_ts") or 0)
    assert decided > 0
    # 历史重放（ts 早于忽略决定）→ 不摘；回填 → 不摘（走 note_backfill，不是真开口）
    _push(store, ck, "old replayed", old_ts + 60, "m0")
    assert dr.IGNORED_TAG in store.get_conv_tags(cid), "历史重放不该摘"
    _push(store, ck, "backfilled later", decided + 30, "mb", backfill=True)
    assert dr.IGNORED_TAG in store.get_conv_tags(cid), "回填不该摘"
    # 客户真开口（忽略之后到达）→ 摘标 + 日志
    with caplog.at_level(logging.INFO):
        _push(store, ck, "hey, still there?", decided + 60, "m2")
    assert dr.IGNORED_TAG not in store.get_conv_tags(cid)
    assert any("[dormant] action=unignored" in rec.getMessage() and cid in rec.getMessage()
               for rec in caplog.records)
    # 幂等：再来一条无标可摘，不抛
    _push(store, ck, "hello?", decided + 120, "m3")
    assert dr.IGNORED_TAG not in store.get_conv_tags(cid)
    # 单一写点：全仓只有 ingest_incoming 调 note_real_inbound；标签摘除只在 dormant_review 内
    import re
    src_root = Path(__file__).resolve().parents[1] / "src"
    callers = [p for p in src_root.rglob("*.py")
               if "note_real_inbound(" in p.read_text(encoding="utf-8") and p.name != "dormant_review.py"]
    assert [p.name for p in callers] == ["protocol_bridge.py"], callers
    dr_src = (src_root / "inbox" / "dormant_review.py").read_text(encoding="utf-8")
    assert len(re.findall(r"tags \+ \[IGNORED_TAG\]", dr_src)) == 1        # 加：apply_action 一处
    assert len(re.findall(r"t != IGNORED_TAG", dr_src)) == 1               # 摘：note_real_inbound 一处
    assert "IGNORED_TAG" not in (src_root / "integrations" / "protocol_bridge.py").read_text(encoding="utf-8")


def test_note_real_inbound_without_ts_and_no_row(store):
    """无 dormant 行（标是别处打的）/ 判不出 ts → 按「现在」摘；无标 → 静默。"""
    cid = _conv("u9")["conversation_id"]
    store.set_conv_tags(cid, ["VIP", dr.IGNORED_TAG])
    out = dr.note_real_inbound(store, cid)
    assert out["untagged"] is True and store.get_conv_tags(cid) == ["VIP"]
    out2 = dr.note_real_inbound(store, cid)
    assert out2["untagged"] is False
    assert dr.note_real_inbound(None, cid)["untagged"] is False


# ═══════════════ 例 3：停联确认 → 标进 meta，守卫仍拦 ═══════════════

def test_stop_contact_confirm_moves_tag_to_meta_guard_still_blocks(store):
    ck = "12134989840"
    conv = _conv(ck)
    cid = conv["conversation_id"]
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    store.set_automation_mode(cid, "auto_ai", source="human")
    _push(store, ck, "please stop", time.time() - 60, "s1")
    d1 = svc.auto_generate_draft(conv, "please stop", automation_mode="auto_ai", enrich=True)
    assert d1 and sc.is_farewell_draft(store.get_draft(d1))
    assert sc.frozen_reason(store, cid) == "stop_contact"
    tags = store.get_conv_tags(cid)
    assert HANDOFF_TAG in tags and sc.STOP_CONTACT_TAG in tags
    assert store.get_stop_contact_at(cid) == 0.0
    # 坐席确认「已处理」
    out = sc.confirm_stop_contact(store, cid, actor="agent_ack:zl", now=1_800_000_000.0)
    assert out["ok"] and out["untagged"] and out["unlabelled"] and out["meta_set"] and out["archived"]
    tags2 = store.get_conv_tags(cid)
    assert sc.STOP_CONTACT_TAG not in tags2 and HANDOFF_TAG not in tags2
    assert store.get_stop_contact_at(cid) == 1_800_000_000.0
    assert store.get_conv_meta(cid)["archived"] == 1
    assert store.get_handoff_meta(cid) == {}
    # 守卫不弱化：仍冻结、档位仍 manual、第二条不起草（告别「最多一条」）
    assert sc.frozen_reason(store, cid) == "stop_contact"
    assert store.get_automation_mode_if_set(cid) == "manual"
    d2 = svc.auto_generate_draft(conv, "so stop writing to me", automation_mode="auto_ai", enrich=True)
    assert d2 is None
    pend = [d for d in store.list_drafts(conversation_id=cid, limit=20)
            if d.get("status") in ("pending", "enriching")]
    assert [d["draft_id"] for d in pend] == [d1]
    # 幂等：再确认不覆盖首次时刻
    out2 = sc.confirm_stop_contact(store, cid, actor="agent_ack:zl", now=1_900_000_000.0)
    assert out2["ok"] and out2["meta_set"] is False and store.get_stop_contact_at(cid) == 1_800_000_000.0
    # 客户日后再来消息 → 自动解档回列表，但冻结不变（AI 仍沉默）
    _push(store, ck, "hi again", time.time() + 5, "s2")
    assert store.get_conv_meta(cid)["archived"] == 0
    assert sc.frozen_reason(store, cid) == "stop_contact"
    assert sc.STOP_CONTACT_TAG not in store.get_conv_tags(cid)
    # split_targets 仍归 frozen 栏（沉寂清单 / 切档确认框同判据）
    sp = dr.split_targets(store, PLAT, ACCT, [cid])
    assert sp["counts"]["frozen"] == 1 and sp["counts"]["active"] == 0
    # 人工解冻才清 meta → 解冻
    un = sc.unfreeze_conversation(store, cid, actor="agent:zl")
    assert un["meta_cleared"] is True and store.get_stop_contact_at(cid) == 0.0
    assert sc.frozen_reason(store, cid) == ""
    assert un["mode_restored"] == "auto_ai"


def test_confirm_on_legacy_store_without_meta_keeps_tag(store, monkeypatch):
    """旧 store 没有 stop_contact_at：标签是唯一冻结判据 → 不摘（否则＝解冻），只归档摘需人工，ok=False。"""
    ck = "u7"
    cid = _conv(ck)["conversation_id"]
    sc.freeze_conversation(store, platform=PLAT, account_id=ACCT, chat_key=ck,
                           conversation_id=cid, reason="stop_contact", hits=["leave me alone"])
    monkeypatch.delattr(InboxStore, "set_stop_contact_at", raising=True)
    monkeypatch.delattr(InboxStore, "get_stop_contact_at", raising=True)
    out = sc.confirm_stop_contact(store, cid, actor="agent_ack")
    assert out["ok"] is False and out["error"] == "no_stop_contact_meta"
    assert sc.STOP_CONTACT_TAG in store.get_conv_tags(cid) and HANDOFF_TAG not in store.get_conv_tags(cid)
    assert sc.frozen_reason(store, cid) == "stop_contact"
    assert out["archived"] is True


def test_frozen_reason_reads_meta_and_tolerates_mock():
    from unittest.mock import MagicMock
    assert sc.frozen_reason(MagicMock(), "x") == ""
    assert sc.stop_contact_at(MagicMock(), "x") == 0.0
    assert sc.stop_contact_at(None, "x") == 0.0


def test_confirm_route_contract_and_wiring(store, monkeypatch):
    """POST /api/workspace/conv/{cid}/stop-contact/confirm 注册在标签路由域，走 confirm_stop_contact。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import src.web.routes.unified_inbox_workspace_tags_routes as mod

    def _noauth(request: Request):   # Depends() 需要 Request 标注，裸 lambda 会被当 query 参数 → 422
        return None

    app = FastAPI()
    mod.register_workspace_tags_routes(app, api_auth=_noauth)
    live = {(getattr(r, "path", ""), m) for r in app.routes
            for m in (getattr(r, "methods", None) or set()) if m not in {"HEAD", "OPTIONS"}}
    assert ("/api/workspace/conv/{conversation_id}/stop-contact/confirm", "POST") in live
    ck = "u5"
    cid = _conv(ck)["conversation_id"]
    sc.freeze_conversation(store, platform=PLAT, account_id=ACCT, chat_key=ck,
                           conversation_id=cid, reason="stop_contact", hits=["stop texting"])
    monkeypatch.setattr(mod, "_inbox_store", lambda request: store)
    with TestClient(app) as c:
        r = c.post(f"/api/workspace/conv/{cid}/stop-contact/confirm", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["archived"] is True and body["frozen"] is True
    assert sc.STOP_CONTACT_TAG not in body["tags"] and HANDOFF_TAG not in body["tags"]
    assert body["stop_contact_at"] > 0
    assert sc.frozen_reason(store, cid) == "stop_contact"
