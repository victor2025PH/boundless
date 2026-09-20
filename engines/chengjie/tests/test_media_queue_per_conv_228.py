# -*- coding: utf-8 -*-
"""#228（M-3 C，2026-09-06，V9DZ5S 实锤）：待发附件按会话隔离。

事故：LINE tisay→Kevin 会话里发失败的 IMG_7312.MOV 切到 WhatsApp Nori→Nell 会话后仍挂在
输入框上方——_mediaQueue / _pendingMedia 是全局变量，点发送就把 Kevin 的文件发给 Nell。
文字草稿早已按会话隔离（_convDraftSwap），只有媒体队列漏了。

守四条：① 队列按 convKey 存、selectChat 与草稿同位交换；② 入队即钉归属（convKey + 平台·
客户名），附件条标「发往 …」；③ _doSendMedia 整趟钉在点发送那一刻的会话/队列上，发送前校验
归属不符即拦；④ 失败件可一键清除。静态断言（模板热更新即上生产，无浏览器门禁）。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_I18N = _ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py"
_CSS = _ROOT / "src" / "web" / "static" / "workspace" / "sticker-panel.css"


def _fn(src: str, name: str) -> str:
    i = src.find(name)
    assert i > 0, f"{name} 失踪"
    j = src.find("\n}\n", i)
    return src[i:j]


def test_media_queue_is_per_conversation_and_swapped_with_draft():
    src = _TPL.read_text(encoding="utf-8")
    assert "const _mediaQueues={};" in src
    swap = _fn(src, "function _mediaQueueSwap(next)")
    assert "_mediaQueues[k]=_mediaQueue" in swap and "_mediaQueue=(nk&&_mediaQueues[nk])||[]" in swap
    # selectChat：与草稿交换同位、在 selectedChat 被覆盖之前
    sel = _fn(src, "function selectChat(key)")
    i_draft, i_media, i_assign = (sel.find("_convDraftSwap(c)"), sel.find("_mediaQueueSwap(c)"),
                                  sel.find("selectedKey=key; selectedChat=c;"))
    assert 0 < i_draft < i_media < i_assign


def test_stage_pins_owner_and_bar_shows_target():
    src = _TPL.read_text(encoding="utf-8")
    stage = _fn(src, "function _stageMedia(file)")
    assert "convKey:ck" in stage and "target:{platform:selectedChat.platform" in stage
    bar = _fn(src, "function _renderMediaBar()")
    assert "inbox.media.target_to" in bar and "data-mpi-clear-failed" in bar
    assert "function _clearFailedMedia()" in src
    # 清空必须原地（_mediaQueues 存的是同一引用）
    cancel = _fn(src, "function _cancelMedia()")
    assert "_mediaQueue.length=0" in cancel and "_mediaQueue=[]" not in cancel


def test_send_loop_pinned_to_click_time_conversation():
    src = _TPL.read_text(encoding="utf-8")
    send = _fn(src, "async function _doSendMedia(caption)")
    assert "const chat=selectedChat, q=_mediaQueue, ck=convKey(chat);" in send
    assert "item.convKey!==ck" in send and "inbox.media.wrong_conv" in send
    assert "_xhrSendMedia(item, sent===0?(caption||''):'', chat)" in send
    assert "q.shift()" in send and "_mediaQueue.shift()" not in send
    assert "function _xhrSendMedia(item,caption,chatArg)" in src
    assert "const chat=chatArg||selectedChat;" in src


def test_i18n_and_css_present():
    pack = _I18N.read_text(encoding="utf-8")
    for k in ("inbox.media.target_to", "inbox.media.clear_failed_n", "inbox.media.wrong_conv"):
        assert pack.count(f'"{k}"') == 2, k
    css = _CSS.read_text(encoding="utf-8")
    assert ".mpb-head{" in css and ".mpb-target{" in css
    tpl = _TPL.read_text(encoding="utf-8")
    assert "sticker-panel.css?v=20260817b" not in tpl, "改了静态 CSS 必须撬缓存"
