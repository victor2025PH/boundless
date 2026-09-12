# -*- coding: utf-8 -*-
"""Q-35 #306（BPMEWX）C 段：相册无命中**坐席侧**可见——marker → conv_state.notes → 状态带琥珀一行 + 深链。

AI 侧「无命中不沉默 / 改口」（``note_album_miss`` → ``album_miss_addendum``）已在库，本门禁只钉新增的
旁路：① ``album_miss_marker`` 写 / 累计 / 命中即清 / 24h 陈旧；② ``image_autosend.pick_registered_media``
``candidates=0 && 索图`` 处写 note、命中处清（Q-6 打分与 addendum 行为不变——同批断言 ``note_album_miss``
仍被调用）；③ ``conv_state.compute`` 只读第七源进 ``notes``（**不进 sources、不改 state**，compute 零写）；
④ 模板：``unified_inbox.html`` ``cs-note`` 行 + ``personas.html`` ``?pid=&pma=1&filter=notrg`` 深链消费；
⑤ 词条 zh / en / 繁体齐平。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from src.inbox import album_miss_marker as amm
from src.inbox.conv_state import compute

_ROOT = Path(__file__).resolve().parents[1]
CID = "whatsapp:acct1:peer7"


class _Store:
    def __init__(self, mode="auto_ai"):
        self.kv = {}
        self.mode = mode
        self.writes = 0

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.writes += 1
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value

    def get_automation_mode(self, cid):
        return self.mode

    def get_conv_tags(self, cid):
        return []

    def get_handoff_meta(self, cid):
        return {}


class _Health:
    def dump(self):
        return {"inbox_health": {}, "sessions": {}}


# ── ① marker ────────────────────────────────────────────────────────────

def test_marker_mark_get_clear_and_repeat_count():
    st = _Store()
    t0 = 1_700_000_000.0
    rec = amm.mark(CID, query="发张自拍给我看看呗\n谢谢", scene="selfie", persona_id="lin", store=st, ts=t0)
    assert rec and rec["n"] == 1 and rec["query"] == "发张自拍给我看看呗 谢谢" and rec["persona_id"] == "lin"
    raw = json.loads(st.kv[amm.KEY_PREFIX + CID])
    assert raw["ts"] == t0 and raw["first_ts"] == t0
    # 同会话再次无命中（6h 内）→ n 累计、first_ts 不变
    rec2 = amm.mark(CID, query="再来一张", persona_id="lin", store=st, ts=t0 + 600)
    assert rec2["n"] == 2 and rec2["first_ts"] == t0 and rec2["query"] == "再来一张"
    got = amm.get(CID, store=st, now=t0 + 700)
    assert got and got["n"] == 2 and got["age_sec"] == 100
    # 24h 后陈旧 → 不显示（不删）
    assert amm.get(CID, store=st, now=t0 + 600 + amm.DEFAULT_TTL_SEC + 1) is None
    assert amm.KEY_PREFIX + CID in st.kv
    # 清：有 note 才写；再清零写
    w = st.writes
    assert amm.clear(CID, store=st) is True and (amm.KEY_PREFIX + CID) not in st.kv
    assert amm.clear(CID, store=st) is False and st.writes == w + 1


def test_marker_without_store_is_noop(monkeypatch):
    monkeypatch.setattr(amm, "_default_store", lambda: None)
    assert amm.mark(CID, query="自拍") is None
    assert amm.get(CID) is None
    assert amm.clear(CID) is False
    assert amm.mark("", query="x", store=_Store()) is None


def test_deep_link_quotes_pid():
    assert amm.deep_link("lin") == "/personas?pid=lin&pma=1&filter=notrg"
    assert amm.deep_link("a b/#c") == "/personas?pid=a%20b%2F%23c&pma=1&filter=notrg"
    assert amm.deep_link("") == "/personas?pma=1&filter=notrg"


# ── ② image_autosend 接线 ────────────────────────────────────────────────

def _row(i, **kw):
    r = {"id": str(i), "enabled": True, "media_type": "photo", "triggers": kw.get("triggers", []),
         "weight": 1, "tags": kw.get("tags", []), "auto_meta": kw.get("auto_meta", {}),
         "min_bond_level": 0, "file_path": "", "url": ""}
    r.update(kw)
    return r


class _AlbumSt:
    def __init__(self, rows):
        self.rows = rows

    def list(self, *a, **k):
        return list(self.rows)

    def sent_history(self, *a, **k):
        return {"ids": set(), "series": set(), "file_keys": set(), "items": []}


def test_pick_registered_media_marks_on_miss_and_clears_on_hit(monkeypatch):
    from src.inbox import image_autosend as ia

    inbox = _Store()
    monkeypatch.setattr(amm, "_default_store", lambda: inbox)
    album = _AlbumSt([_row(1, triggers=["跳舞"], tags=["kind:selfie"])])
    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: album)
    cfg = {"companion": {"selfie": {"enabled": True}}}
    ck = "whatsapp:acct1:miss1"
    ia.consume_album_miss(ck)
    # 索风景照 → candidates=0 → Q-6 进程内 note 照旧 + 新持久 note
    assert ia.pick_registered_media(cfg, "nori", "发张风景照", conv_key=ck) is None
    q6 = ia.consume_album_miss(ck)
    assert q6 and q6.get("scene") == "outdoor", "Q-6 note_album_miss 行为不得改变"
    rec = amm.get(ck, store=inbox)
    assert rec and rec["query"] == "发张风景照" and rec["persona_id"] == "nori" and rec["scene"] == "outdoor"
    # 非索图的闲聊无命中 → 不记（宁缺勿滥）
    ck2 = "whatsapp:acct1:chat"
    assert ia.pick_registered_media(cfg, "nori", "在吗", conv_key=ck2) is None
    assert amm.get(ck2, store=inbox) is None
    # 运营补了触发词 → 同句命中 → note 清
    album.rows = [_row(1, triggers=["跳舞"]), _row(2, triggers=["风景照"], tags=["kind:outdoor"])]
    row = ia.pick_registered_media(cfg, "nori", "发张风景照", conv_key=ck)
    assert row and row["id"] == "2"
    assert amm.get(ck, store=inbox) is None
    # 无 conv_key（A 线部分调用）→ 不记不清、不抛
    assert ia.pick_registered_media(cfg, "nori", "发张夜景照") is None


# ── ③ conv_state 只读第七源 → notes ─────────────────────────────────────

def _c(store, **kw):
    kw.setdefault("platform", "whatsapp")
    kw.setdefault("account_id", "acct1")
    kw.setdefault("health", _Health())
    kw.setdefault("config", {})
    return compute(store, CID, **kw)


def test_conv_state_notes_carry_album_no_match_without_changing_state():
    st = _Store("auto_ai")
    now = time.time()
    amm.mark(CID, query="发张自拍", scene="selfie", persona_id="lin", store=st, ts=now - 120)
    w = st.writes
    out = _c(st, now=now)
    assert st.writes == w, "compute 必须零写"
    assert out["state"] == "auto" and out["will_send"] is True and out["tone"] == "ok", "note 不改状态"
    assert "album_miss" not in out["sources"], "六源契约不动，note 只走 notes"
    assert isinstance(out["notes"], list) and len(out["notes"]) == 1
    n = out["notes"][0]
    assert n["kind"] == "album_no_match" and n["query"] == "发张自拍" and n["persona_id"] == "lin"
    assert n["n"] == 1 and n["link"] == "/personas?pid=lin&pma=1&filter=notrg"
    assert n["text_key"] == "inbox.cs.note.album_no_match" and re.fullmatch(r"\d\d:\d\d", n["hhmm"])
    assert n["ago_sec"] == 120
    # 人审档也带（相册没图与档位无关）
    st2 = _Store("review")
    amm.mark(CID, query="selfie pls", persona_id="lin", store=st2, ts=now - 5)
    out2 = _c(st2, now=now)
    assert out2["state"] == "human" and out2["notes"][0]["kind"] == "album_no_match"


def test_conv_state_notes_empty_when_none_or_stale_or_bad_store():
    st = _Store("auto_ai")
    now = time.time()
    assert _c(st, now=now)["notes"] == []
    amm.mark(CID, query="自拍", persona_id="lin", store=st, ts=now - amm.DEFAULT_TTL_SEC - 60)
    assert _c(st, now=now)["notes"] == []
    st.kv[amm.KEY_PREFIX + CID] = "{not json"
    assert _c(st, now=now)["notes"] == []

    class _Bad(_Store):
        def get_app_setting(self, key, default=""):
            raise RuntimeError("db gone")

    out = _c(_Bad("auto_ai"), now=now)
    assert out["notes"] == [] and out["state"] == "auto"


# ── ④ 模板契约 ───────────────────────────────────────────────────────────

def test_inbox_template_renders_note_band_and_exposes_action():
    s = (_ROOT / "src/web/templates/unified_inbox.html").read_text(encoding="utf-8", errors="replace")
    assert 'id="cs-note"' in s and 'class="cs-band cs-warn cs-s-note"' in s, "cs-note 行缺失 / 未复用 cs-band 琥珀样式"
    assert 'onclick="_csNoteAction(this)"' in s and "window._csNoteAction=_csNoteAction;" in s
    body = s[s.index("function _csRender(){"):s.index("function _csSyncLegacy(")]
    assert body.count("_csRenderNotes(") >= 2, "隐藏分支与显示分支都要刷 notes（否则切会话残留）"
    fn = s[s.index("function _csRenderNotes("):s.index("window._csNoteAction=")]
    assert "'album_no_match'" in fn and "inbox.cs.note.album_no_match_n" in fn and "inbox.cs.note.album_go" in fn
    assert "link.charAt(0)!=='/'" in fn, "深链只认站内相对路径"
    assert "&pma=1&filter=notrg" in fn


def test_personas_template_consumes_deep_link():
    s = (_ROOT / "src/web/templates/personas.html").read_text(encoding="utf-8", errors="replace")
    assert "_qs.get('pma') === '1'" in s and "_pendingHashTab = 'album'" in s
    assert "var _pmaPendingFilter = ''" in s
    load = s[s.index("async function pmaLoad("):s.index("function _pmaRenderGuide(")]
    assert "_pmaFilter = _pmaPendingFilter" in load, "pmaLoad 首次渲染必须消费预置筛选"
    assert "['notrg', 'nohit', 'off', 'untagged', 'photo', 'video'].indexOf(_pf)" in s, "filter 只认相册筛选枚举"


# ── ⑤ 词条 ───────────────────────────────────────────────────────────────

def test_note_i18n_keys_bilingual_and_traditional():
    import importlib
    pack = importlib.import_module("src.web.i18n_packs.conv_state_q26")
    keys = ("inbox.cs.note.album_no_match", "inbox.cs.note.album_no_match_n",
            "inbox.cs.note.album_no_match_t", "inbox.cs.note.album_go",
            "inbox.cs.note.album_go_t", "inbox.cs.note.album_q_generic")
    for k in keys:
        assert k in pack.ZH and k in pack.EN and k in pack.ZH_HANT, k
        ph = lambda d: set(re.findall(r"\{(\w+)\}", d[k]))  # noqa: E731
        assert ph(pack.ZH) == ph(pack.EN) == ph(pack.ZH_HANT), k
    assert {"q", "hhmm"} <= set(re.findall(r"\{(\w+)\}", pack.ZH["inbox.cs.note.album_no_match"]))
    assert "改口" in pack.ZH["inbox.cs.note.album_no_match"] and "补标签" in pack.ZH["inbox.cs.note.album_go"]
