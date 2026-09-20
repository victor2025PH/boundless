# -*- coding: utf-8 -*-
"""K-3 B（#183 #177，J-10 A3 交办）：草稿旁「本轮用了 N 条记忆」chips 静态钉。

后端契约在 ``docs/发版对账_v1.0.74_J10.md``「交 J-4」：``GET /api/episodic-memory/used`` +
``POST /api/episodic-memory/{id}/ignore``。前端只消费、不改接口。这里钉住：
  * 两个草稿面（待审面板 mini 卡 / composer 草稿条）都有 ``.mem-chips`` 容器且都接了填充；
  * 接口路径字面（换路径必须同步改这里）+ 旧后端 404 探测（整块不出现）；
  * 串轮对账两道（inbound_msg_id ↔ 会话流最新入站 data-mid；草稿 created_ts ≥ 批次 ts）；
  * 「不再使用」/「来源原话定位」走容器事件委托、零内联 handler（J-7 C 门禁形态）；
  * 函数挂 window（运行时守卫 / 门禁可达）；i18n 键 zh/en 齐、``{n}`` 占位在；
  * 双戳：``unified-inbox.css?v=`` 已 bump、CSS 里有 chips 样式且用的是工作台已定义 token。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_CSS = _ROOT / "src" / "web" / "static" / "workspace" / "unified-inbox.css"


@pytest.fixture(scope="module")
def tpl() -> str:
    return _TPL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return _CSS.read_text(encoding="utf-8")


def test_both_draft_surfaces_have_containers_and_fill_hooks(tpl):
    # composer 草稿条：静态容器 + 渲染时挂 chips + 无稿时清空
    assert '<div class="mem-chips" id="cdraft-mem"></div>' in tpl
    assert "_memChipsAttach(document.getElementById('cdraft-mem'), d, selectedChat)" in tpl
    assert "_mm.innerHTML=''; _mm.removeAttribute('data-mem-draft');" in tpl
    # 待审面板 mini 卡：每张卡一个容器（带 draft_id / created_ts 供对账），渲染完统一填
    assert 'class="mem-chips" data-mem-draft="${esc(dr.draft_id||\'\')}" data-mem-created="${esc(dr.created_ts||\'\')}"' in tpl
    assert "_memChipsFillAll(items, c, drafts)" in tpl
    # 容器位置＝草稿正文之后、动作条（发送按钮）之前
    i_txt = tpl.index('<div class="draft-text-mini">')
    i_mem = tpl.index('class="mem-chips" data-mem-draft=')
    i_acts = tpl.index('<div class="draft-acts">', i_txt)
    assert i_txt < i_mem < i_acts


def test_api_paths_literal_and_old_backend_probe(tpl):
    assert "'/api/episodic-memory/used?conversation_id='+enc(cid)+'&limit=1'" in tpl
    assert "'/api/episodic-memory/'+enc(id)+'/ignore'" in tpl
    # 旧后端（1.0.74 之前）无接口 → 本会话期不再请求、整块不出现
    assert "if(r.status===404){ _memUsed.unsupported=true; return null; }" in tpl
    assert "if(!cid||_memUsed.unsupported) return null;" in tpl
    # count==0 / 无 batches → 不渲染空 chips 条
    assert "Number(d.count)>0&&Array.isArray(d.batches)&&d.batches.length" in tpl


def test_round_reconciliation_guards(tpl):
    fn = tpl[tpl.index("function _memBatchMatchesDraft(b, dr)"):tpl.index("function _memVisibleItems(b)")]
    assert "if(bmid && lastIn && bmid!==lastIn) return false;" in fn      # 批次属于别的轮次
    assert "if(bts && cts && cts < bts-5) return false;" in fn            # 老稿不配新批
    # 最新入站 id 与 record_recall 的 inbound_msg_id 同源＝store message_id（.msg-row[data-mid]）
    assert "area.querySelectorAll('.msg-row.in[data-mid]')" in tpl
    # 已硬删 / 已不再使用的行不显示
    assert "!it.deleted && String(it.status||'active')!=='ignored'" in tpl


def test_chip_semantics(tpl):
    fn = tpl[tpl.index("function _memChipsHtml(b)"):tpl.index("async function _memChipsAttach(")]
    assert "String(it.impact||'')==='high'" in fn                         # 红点
    assert "String(it.source||'')==='ai_inferred'" in fn                  # 琥珀点
    assert "String(it.review_reason||'').trim()" in fn                    # 「需要你看」小标
    assert "window.Tf('inbox.mem.used_n',{n:items.length})" in fn
    for k in ("inbox.mem.ignore", "inbox.mem.ignore_t", "inbox.mem.need_review",
              "inbox.mem.high_impact", "inbox.mem.src_inferred", "inbox.mem.src_stated",
              "inbox.mem.quote_t"):
        assert f"window.T('{k}')" in fn, k


def test_actions_use_event_delegation_not_inline_handlers(tpl):
    blk = tpl[tpl.index("/* ===== K-3 B（#183 #177"):tpl.index("/* ===== 二期：Composer 一体化 AI 草稿条")]
    # chips 由 innerHTML 拼出（content / source_quote 含引号、JSON），绝不进 on* 属性
    assert "onclick=" not in blk
    assert 'class="mem-chip-ignore" data-mem-id=' in blk
    assert 'class="mem-chip-quote" data-mem-quote=' in blk
    assert "t.closest('.mem-chips .mem-chip-ignore')" in blk
    assert "t.closest('.mem-chips .mem-chip-quote')" in blk
    # 不再使用：调 ignore 后该 chip 消失、N 同步、空了整块收起；403 如实提示
    assert "_memChipIgnore(ig.getAttribute('data-mem-id')||'', ig)" in blk
    assert "if(r.status===403){ _toast(window.T('inbox.mem.ignore_forbidden')" in blk
    assert "if(left<=0){ const host=box.parentElement; box.remove(); if(host) host.innerHTML=''; }" in blk
    # 来源原话定位：前 20 字 + source_ts ±60s + 复用 _flashMsgEl；没加载到 toast，不做历史回拉
    assert "const q=String(quote||'').trim().slice(0,20);" in blk
    assert "Math.abs(t-ts0)<=60" in blk
    assert "_flashMsgEl(hit)" in blk
    assert "_toast(window.T('inbox.mem.locate_miss'),'#b45309')" in blk
    assert "loadOlderThread" not in blk


def test_functions_exposed_on_window(tpl):
    for fn in ("_memChipsAttach", "_memChipsFillAll", "_memChipIgnore", "_memLocateQuote",
               "_memUsedBatch", "_memBatchMatchesDraft"):
        assert re.search(rf"window\.{fn}\s*=\s*{fn}\b", tpl), fn


def test_i18n_keys_bilingual_with_placeholder():
    from src.web.i18n_packs.inbox_workspace import EN, ZH

    keys = ("inbox.mem.used_n", "inbox.mem.ignore", "inbox.mem.ignore_t", "inbox.mem.ignore_ok",
            "inbox.mem.ignore_fail", "inbox.mem.ignore_forbidden", "inbox.mem.need_review",
            "inbox.mem.src_stated", "inbox.mem.src_inferred", "inbox.mem.high_impact",
            "inbox.mem.quote_t", "inbox.mem.locate_miss")
    for k in keys:
        assert str(ZH.get(k) or "").strip(), k
        assert str(EN.get(k) or "").strip(), k
    assert "{n}" in ZH["inbox.mem.used_n"] and "{n}" in EN["inbox.mem.used_n"]


def test_css_double_stamp_and_tokens(tpl, css):
    # ?v= 已 bump 到 2026-09-05 批次（改 CSS 不 bump＝坐席旧标签页看不到样式）
    m = re.search(r'unified-inbox\.css\?v=(\w+)', tpl)
    assert m and m.group(1) >= "20260905d", m and m.group(1)
    assert ".mem-chips:empty{display:none;}" in css
    assert ".cdraft-bar.slim .mem-chips{display:none;}" in css
    for sel in (".mem-chips-box", ".mem-chips-sum", ".mem-chip", ".mem-chip .mem-dot.inferred",
                ".mem-chip .mem-dot.high", ".mem-chip-review", ".mem-chip-quote", ".mem-chip-ignore"):
        assert sel + "{" in css, sel
    blk = css[css.index(".mem-chips:empty"):css.index(".mem-chip-ignore:disabled")]
    # 色点只用工作台已定义 token：--green/--red 在 unified_inbox.html 主题块，--tk-amber-ink 在
    # workspace_base 别名；不写裸 Tailwind 蓝、不引用工作台上下文里不存在的 --amber
    assert "var(--green)" in blk and "var(--red)" in blk and "var(--tk-amber-ink)" in blk
    assert "var(--amber" not in blk
    assert not re.search(r"#(?:3b82f6|2563eb|60a5fa|dbeafe)", blk, re.I)


def test_contract_fields_match_backend_recent_recalls():
    """前端读的字段名必须是后端 recent_recalls 真给的（防契约漂移）。"""
    import inspect

    from src.utils import episodic_memory_store as S
    src = inspect.getsource(S.EpisodicMemoryStore.recent_recalls) + S.EpisodicMemoryStore._ROW_COLS
    for k in ("batch_id", "ts", "chain", "inbound_msg_id", "conversation_id", "items", "deleted"):
        assert f'"{k}"' in src, k
    for col in ("source_quote", "source_ts", "status", "review_reason", "impact", "recall_count"):
        assert col in src, col
