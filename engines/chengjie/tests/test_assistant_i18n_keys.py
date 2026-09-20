# -*- coding: utf-8 -*-
"""小智面板自带词典的「t() 缺键」门禁（2026-08-28 事故沉淀）。

事故：`assistant-ball.js::renderChatIntro` 调 `t('hello')` 渲染问答模式首屏
第一句，而 `I18N.zh` / `I18N.en` **都没有 hello 这个键**；该文件的 t() 兜底是
`d[k] || I18N.zh[k] || k` —— 缺键**回落裸键名**，于是面板打开后用户看到的
第一句话就是一个变量名 "hello"，从 08-23 起活了五天没人发现（老板截图报
「没有引导指示」时才被查出）。

为什么此前没有门禁抓得住：模板侧 `window.T('x')` 的键有全库门禁
（test_template_window_t_keys_resolve）守着，但 shared/assistant/*.js 走的是
**自带词典**（cp-i18n 模式，模板零键）的另一套体系，不在任何门禁的扫描面内。

口径：只检查**字面量**调用 `t('k')` / `t("k")`。动态拼接（`t('st_' + s)`）
天然不匹配，刻意不管——那类键的正确性由 test_assistant_ball_status_keys 一族
的语义门禁负责，在这里用前缀白名单猜反而会放过真缺键。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import _inline_handler_scan as scan

_SHARED = Path(__file__).resolve().parents[1] / "shared" / "assistant"
_FILES = ("assistant-ball.js", "assistant-agent.js", "assistant-teach.js")
_LANGS = ("zh", "en")

_T_CALL = re.compile(r"\bt\(\s*['\"]([A-Za-z_$][\w$]*)['\"]\s*\)")


def _brace_span(masked: str, open_at: int) -> tuple[int, int] | None:
    """从 masked[open_at] == '{' 起做深度匹配，返回 (体开始, 体结束) 下标。"""
    if open_at < 0 or open_at >= len(masked) or masked[open_at] != "{":
        return None
    depth = 0
    for i in range(open_at, len(masked)):
        ch = masked[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (open_at + 1, i)
    return None


def _i18n_body(masked: str) -> str:
    m = re.search(r"\bI18N\s*=\s*\{", masked)
    if not m:
        return ""
    span = _brace_span(masked, m.end() - 1)
    return masked[span[0]:span[1]] if span else ""


def _lang_keys(masked: str, lang: str) -> set[str]:
    """I18N.<lang> 直属键名集合（只认字典本级，不下钻嵌套对象）。"""
    body = _i18n_body(masked)
    if not body:
        return set()
    m = re.search(r"\b" + lang + r"\s*:\s*\{", body)
    if not m:
        return set()
    span = _brace_span(body, m.end() - 1)
    if not span:
        return set()
    inner = body[span[0]:span[1]]
    keys: set[str] = set()
    depth = 0
    for mm in re.finditer(r"([{}])|([A-Za-z_$][\w$]*)\s*:", inner):
        if mm.group(1) == "{":
            depth += 1
        elif mm.group(1) == "}":
            depth -= 1
        elif mm.group(2) is not None and depth == 0:
            keys.add(mm.group(2))
    return keys


def _called_keys(src: str) -> set[str]:
    """源码里 t('…') 字面量调用的键（先剥注释，免得注释里的示例算进来）。"""
    body = scan._strip_js_block_comments(src)
    body = re.sub(r"//[^\n]*", "", body)
    return set(_T_CALL.findall(body))


@pytest.mark.parametrize("fname", _FILES)
def test_every_literal_t_key_exists_in_both_langs(fname):
    path = _SHARED / fname
    if not path.exists():
        pytest.skip(f"{fname} 不存在（组件未随本批交付）")
    src = path.read_text(encoding="utf-8")
    masked = scan._mask(src)
    called = _called_keys(src)
    assert called, f"{fname} 里一个 t('…') 都没扫到——扫描器可能失效了"
    for lang in _LANGS:
        keys = _lang_keys(masked, lang)
        assert keys, f"{fname} 的 I18N.{lang} 没解析出任何键"
        missing = sorted(called - keys)
        assert not missing, (
            f"{fname}: 这些键被 t() 调用但 I18N.{lang} 里没有 → 运行时会把"
            f"**裸键名**显示给用户（t() 缺键回落 k）：{missing}"
        )


def test_scanner_would_catch_the_hello_regression():
    """探测器自证：把 hello 从词典里摘掉，门禁必须变红。

    没有这条，上面那些断言可能因为解析器悄悄退化（比如 I18N 段没解析出来）
    而变成永远为真的摆设——这正是 hello 能活五天的同类风险。
    """
    path = _SHARED / "assistant-ball.js"
    src = path.read_text(encoding="utf-8")
    assert "hello:" in src, "assistant-ball.js 的 hello 键不见了（本门禁的靶子）"
    tampered = src.replace("hello:", "hello_TAMPERED:", 1)
    keys = _lang_keys(scan._mask(tampered), "zh")
    called = _called_keys(tampered)
    assert "hello" in called, "t('hello') 调用点也一起没了？靶子失效"
    # 与主断言逐字同构：这里为真 ⇔ 上面那条 parametrize 断言会红。
    missing = sorted(called - keys)
    assert "hello" in missing, (
        "篡改词典后门禁仍判通过 —— 解析器已退化成摆设"
    )
