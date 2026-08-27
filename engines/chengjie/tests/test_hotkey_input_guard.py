"""单键快捷键「正在输入」守卫的 Shadow DOM 穿透钉（2026-08-20 内测实锤）。

事故：坐席在右栏「跨平台档案」表单（Shadow DOM 组件）里打字，document 层
keydown 的 ``e.target`` 被重定向成组件宿主（<cp-origin>，非 INPUT/TEXTAREA）
→ 旧守卫 ``e.target.tagName`` 判定穿透 → 字母 S 触发「搁置」/E 触发「归档」
→ 会话从列表消失，用户报「聊天列表丢了」（B8/B10 的真根因，用户自己定位到
快捷键冲突）。

修复＝``_hotkeyInEditable``：composedPath()[0] 穿透 open shadow root +
activeElement.shadowRoot 逐层下钻兜底。本钉守住三点：守卫函数存在、
快捷键主处理器消费它、不再退回裸 ``e.target`` 判定。
"""
import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1]
        / "src" / "web" / "templates" / "unified_inbox.html"
        ).read_text(encoding="utf-8")


def _hotkey_block() -> str:
    i = _SRC.index("===== 键盘快捷键 =====")
    return _SRC[i:i + 2500]


def test_guard_function_exists_with_shadow_piercing():
    assert "function _hotkeyInEditable(" in _SRC
    fn_i = _SRC.index("function _hotkeyInEditable(")
    fn = _SRC[fn_i:fn_i + 1600]
    assert "composedPath" in fn, "丢了 composedPath 穿透——shadow 表单打字又会触发 S/E"
    assert "shadowRoot" in fn, "丢了 activeElement 下钻兜底（closed root / 老内核）"
    assert "SELECT" in fn


def test_main_hotkey_handler_consumes_guard():
    blk = _hotkey_block()
    assert "_hotkeyInEditable(e)" in blk, "快捷键主处理器没接守卫"
    # 不允许退回裸 e.target tagName 判定（那就是事故原样）
    assert not re.search(
        r"const _t=e\.target;\s*\n\s*if\(_t && \(_t\.tagName==='TEXTAREA'", blk), (
        "退回了裸 e.target 判定——Shadow DOM 输入将再次穿透")


def test_destructive_single_keys_still_wired():
    blk = _hotkey_block()
    assert "snoozeConv()" in blk and "toggleArchive()" in blk, (
        "S/E 快捷键本体不见了（守卫修复不应顺手拆功能）")
