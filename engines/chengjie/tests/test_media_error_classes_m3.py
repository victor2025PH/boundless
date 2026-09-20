# -*- coding: utf-8 -*-
"""M-3 D（GAJ5T2 ④，2026-09-06）：媒体发送错误四分类 + 无响应探活/自动重试。

四类：格式不支持(415) / 超过上限(413) / 后端明确失败(其余状态码) / 后台无响应(无 HTTP 结果)。
无响应再分：重试中 → 后台在但两次都发不出去(not_sent，请重新选择文件) / 后台一直不在(结果未知)。
在 #164 / #194 的两次「无响应文案」改法之上收口，不再改第三次：retry_hint_unknown /
send_result_unknown 两键原样保留。sticker-panel.js failToast 同口径。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_STK = _ROOT / "src" / "web" / "static" / "workspace" / "sticker-panel.js"
_CSS = _ROOT / "src" / "web" / "static" / "workspace" / "sticker-panel.css"
_I18N = _ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py"


def _fn(src: str, name: str) -> str:
    i = src.find(name)
    assert i > 0, f"{name} 失踪"
    return src[i:src.find("\n}\n", i)]


def test_do_send_media_classifies_four_ways():
    src = _TPL.read_text(encoding="utf-8")
    send = _fn(src, "async function _doSendMedia(caption)")
    assert ("item.errClass=!res?'no_response':(res.status===415?'format':"
            "(res.status===413?'too_large':'failed'))") in send
    assert "res.status===415||res.status===413" in send
    assert "inbox.media.err_format" in send and "inbox.media.err_too_large" in send
    # 无响应：单次自动重试守卫 + 不在该分支弹 toast（#194）
    assert "if(!item.autoRetried){ item.autoRetried=true; try{ _mediaAutoRetry(item,q,chat); }" in send
    tail = send.split("} else {", 1)[-1]
    tail = tail[: tail.find("break;")]
    assert "_toast(" not in tail


def test_item_html_wording_per_class():
    src = _TPL.read_text(encoding="utf-8")
    item = _fn(src, "function _mediaItemHtml(it)")
    for key in ("inbox.media.err_format_hint", "inbox.media.err_too_large_hint",
                "inbox.media.wrong_conv_hint", "inbox.media.no_response_retrying",
                "inbox.media.not_sent_alive", "inbox.media.retry_hint"):
        assert key in item, key
    # #194 的两键原样保留、按 unknownRestart 二选一
    assert "it.unknownRestart?'inbox.media.retry_hint_unknown':'inbox.media.send_result_unknown'" in item
    # 无响应是黄不是红
    assert "(it.netFail?' unknown':'')" in item
    assert ".mpb-item.failed.unknown{" in _CSS.read_text(encoding="utf-8")


def test_auto_retry_probe_and_single_shot():
    src = _TPL.read_text(encoding="utf-8")
    assert "async function _probeBackendAlive(chat)" in src
    retry = _fn(src, "async function _mediaAutoRetry(item,q,chat)")
    assert "_probeBackendAlive(chat)" in retry
    assert "_reconcileGhostMediaFailures('retry')" in retry, "重试前先对账，别双发"
    assert "selectedChat!==chat||_mediaSending" in retry, "切走/在途不替人做主"
    assert "item.errClass='not_sent'" in retry
    assert "await _doSendMedia(" in retry
    # renderer 日志带 error/failed 字样（zl_collect 过滤词）
    assert "auto-retry skipped error=backend_unreachable" in retry
    assert "upload failed twice with backend alive error=not_sent" in retry


def test_i18n_keys_and_sticker_same_taxonomy():
    pack = _I18N.read_text(encoding="utf-8")
    for k in ("inbox.media.err_format", "inbox.media.err_too_large", "inbox.media.err_format_hint",
              "inbox.media.err_too_large_hint", "inbox.media.wrong_conv_hint",
              "inbox.media.no_response_retrying", "inbox.media.not_sent_alive"):
        assert pack.count(f'"{k}"') == 2, k
    stk = _STK.read_text(encoding="utf-8")
    ft = stk[stk.find("function failToast"):]
    ft = ft[: ft.find("\n  }\n")]
    assert "status === 415" in ft and "status === 413" in ft
    assert "inbox.media.err_format" in ft and "inbox.media.err_too_large" in ft
    tpl = _TPL.read_text(encoding="utf-8")
    assert "sticker-panel.js?v=20260906a\"" not in tpl and "sticker-panel.css?v=20260906m3\"" not in tpl, "改静态件必须撬缓存"
