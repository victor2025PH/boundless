"""#164 / #194：媒体发送无 HTTP 响应 → 「结果未知」，且**只出一处提示、按真相措辞**。

#164（J-4 C，钧机 1.0.73 诊断包 3PZ95W）：报障当下 backend.log 零 send_media，红字却是
inbox.media.send_fail_net。桌面包后端自启不走 instance_restart 冷却，_mediaBackendUnstable
判稳 → 旧 else 走失败措辞，坐席据此重发。→ 无响应一律按「未知」。

#194（L-1 D，2026-09-06，9N97AY + 群图 mid 1205）：发视频无响应时附件条「后台刚重启，
结果未知…」+ 右下 toast「后台重启中，媒体发送结果未知…」**同时出现**，而后台并没在
重启。→ ① 只留附件条内联一处（toast 撤）；② 失败瞬间记 item.unknownRestart =
_mediaBackendUnstable()，附件条按真相二选一：真在重启冷却/断连 → 「后台重启中」，
否则 → 「没拿到发送结果（可能是网络或文件较大超时）」；③ 贴纸面板（无附件条）
的 toast 同口径二选一。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_STK = _ROOT / "src" / "web" / "static" / "workspace" / "sticker-panel.js"
_I18N = _ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py"


def _do_send_media_chunk() -> str:
    src = _TPL.read_text(encoding="utf-8")
    start = src.find("async function _doSendMedia")
    assert start > 0, "_doSendMedia 失踪"
    # 截到函数收尾（顶层 `\n}\n`），不按固定字符数——注释一长固定窗口就把分支切在外面
    end = src.find("\n}\n", start)
    assert end > start, "_doSendMedia 没有收尾"
    return src[start:end]


def test_do_send_media_no_response_is_unknown_and_single_prompt():
    chunk = _do_send_media_chunk()
    # 失败键只留给有 HTTP 状态的路径；无响应分支（第一个裸 else，到 break 收尾）不许再叠 toast
    after_else = chunk.split("} else {", 1)[-1]
    after_else = after_else[: after_else.find("break;")]
    assert after_else.strip(), "无响应 else 分支没找到"
    assert "_toast(" not in after_else, "#194：无响应提示只留附件条一处，else 分支不得再弹 toast"
    assert "inbox.media.send_fail_net')" not in after_else
    assert "inbox.media.send_fail_net\"" not in after_else
    # 失败瞬间按真相记标（附件条据此二选一措辞）
    assert "item.unknownRestart=_mediaBackendUnstable()" in chunk
    # 红字仍只属于后端明确返回的失败（带 res 的分支）
    assert "inbox.media.send_fail_status" in chunk


def test_media_item_html_picks_wording_by_truth():
    src = _TPL.read_text(encoding="utf-8")
    i = src.find("function _mediaItemHtml")
    assert i > 0
    end = src.find("\nfunction ", i + 10)
    window = src[i: end if end > i else i + 4000]
    assert ("it.unknownRestart?'inbox.media.retry_hint_unknown'"
            ":'inbox.media.send_result_unknown'") in window, (
        "附件条必须按 unknownRestart 二选一：重启中 / 没拿到发送结果")
    assert "'inbox.media.retry_hint'" in window   # 有状态的失败仍是「发送失败，点发送重试」


def test_unknown_wording_i18n_zh_en_truthful():
    pack = _I18N.read_text(encoding="utf-8")
    assert pack.count('"inbox.media.send_result_unknown"') == 2, "zh/en 两份词表都要有键"
    zh = {}
    for ln in pack.splitlines():
        s = ln.strip()
        for key in ("inbox.media.send_result_unknown", "inbox.media.retry_hint_unknown"):
            if s.startswith(f'"{key}"') and key not in zh:
                zh[key] = s
    # 非重启窗的那句：不得提「重启」（后台并没在重启）
    assert "重启" not in zh["inbox.media.send_result_unknown"]
    assert "没拿到发送结果" in zh["inbox.media.send_result_unknown"]
    # 重启窗的那句：说清「后台重启中」
    assert "后台重启中" in zh["inbox.media.retry_hint_unknown"]


def test_sticker_send_catch_uses_truthful_wording():
    src = _STK.read_text(encoding="utf-8")
    # 贴纸发送 catch（S.sending 复位前）：无附件条，toast 是唯一提示面 → 同口径二选一
    i = src.find("S.sending = false")
    assert i > 0
    window = src[max(0, i - 700):i]
    assert "_mediaBackendUnstable" in window
    assert "send_fail_net_restart" in window and "send_result_unknown" in window
    assert "send_fail_net')" not in window
    # 改了静态 JS 必须撬缓存（模板 script 标签版本号）
    tpl = _TPL.read_text(encoding="utf-8")
    assert "sticker-panel.js?v=20260904a" not in tpl
