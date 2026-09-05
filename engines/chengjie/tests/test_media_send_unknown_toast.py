"""#164：媒体发送无 HTTP 响应必须按「结果未知」，不得再标「发送失败」。

钧机 1.0.73 诊断包 3PZ95W：报障当下 backend.log 零 send_media，红字却是
inbox.media.send_fail_net。桌面包后端自启不走 instance_restart 冷却，
_mediaBackendUnstable 判稳 → 旧 else 走失败措辞，坐席据此重发。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"
_STK = _ROOT / "src" / "web" / "static" / "workspace" / "sticker-panel.js"


def test_do_send_media_no_response_uses_unknown_wording():
    src = _TPL.read_text(encoding="utf-8")
    start = src.find("async function _doSendMedia")
    assert start > 0, "_doSendMedia 失踪"
    # 截到函数收尾（顶层 `\n}\n`），不按固定字符数——注释一长固定窗口就把分支切在外面
    end = src.find("\n}\n", start)
    assert end > start, "_doSendMedia 没有收尾"
    chunk = src[start:end]
    # 无响应分支只许 restart/未知键；失败键只留给有 HTTP 状态的路径
    assert "inbox.media.send_fail_net_restart" in chunk
    # else 不再调用失败键（有状态的分支用 send_fail_status / detail）
    after_else = chunk.split("} else {", 1)[-1]
    assert "inbox.media.send_fail_net_restart" in after_else
    assert "inbox.media.send_fail_net')" not in after_else
    assert "inbox.media.send_fail_net\"" not in after_else


def test_sticker_send_catch_uses_unknown_wording():
    src = _STK.read_text(encoding="utf-8")
    # 贴纸发送 catch（S.sending 复位前）与媒体无响应同口径
    i = src.find("S.sending = false")
    assert i > 0
    window = src[max(0, i - 400):i]
    assert "send_fail_net_restart" in window
    assert "send_fail_net')" not in window
