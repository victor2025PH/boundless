# -*- coding: utf-8 -*-
"""小智「教学模式」姊妹模块接线门禁（实施58 P0，2026-08-23）。

守三条不变量（全静态、零依赖、毫秒级）：
1. **版本戳同步**：assistant-teach.js 内 VER 必须与两壳模板加载器的
   `assistant-teach.js?v=<stamp>` 逐字一致——「改了组件忘 bump 模板」是
   陈旧缓存事故的标准姿势（前端批次双戳纪律的机器化）。
2. **零内联 handler**：本模块生成的 HTML 片段不得出现 `on*=` 内联事件
   （全站哑按钮门禁的同族约束；组件用 addEventListener + data 属性委托）。
3. **内置词典 zh/en 齐平**：`t('key')` 用到的每个键必须在文件里出现
   ≥2 次键定义（zh 与 en 各一份）——防「加了中文忘加英文」的裸键回退。

另钉：模块必须保持对 assistant-ball.js 的**零符号依赖**（只做 DOM 协作）
——形象线并行重构球本体，import/直接调用其内部函数会把两条线焊死。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEACH_JS = ROOT / "shared" / "assistant" / "assistant-teach.js"
TEMPLATES = [
    ROOT / "src" / "web" / "templates" / "base.html",
    ROOT / "src" / "web" / "templates" / "workspace_base.html",
]


def _src() -> str:
    return TEACH_JS.read_text(encoding="utf-8")


def test_teach_js_exists_and_nontrivial():
    assert TEACH_JS.is_file(), "assistant-teach.js 不存在（加载器会静默 404）"
    assert TEACH_JS.stat().st_size > 4000, "文件疑似被截断/空壳"


def test_version_stamp_synced_across_loaders():
    src = _src()
    m = re.search(r"var VER = '([0-9a-z]+)'", src)
    assert m, "assistant-teach.js 缺 VER 声明"
    ver = m.group(1)
    for tpl in TEMPLATES:
        text = tpl.read_text(encoding="utf-8")
        stamps = re.findall(r"assistant-teach\.js\?v=([0-9a-z]+)", text)
        assert stamps, f"{tpl.name} 未接线 assistant-teach.js 加载器"
        assert all(s == ver for s in stamps), (
            f"{tpl.name} 的 ?v={stamps} 与组件 VER={ver} 不同步——"
            "改组件必须同批 bump 两壳模板加载器"
        )


def test_no_inline_event_handlers_in_generated_html():
    src = _src()
    # 组件内所有 HTML 都由字符串拼接产生；内联 on*= 会绕过全站事件委托纪律。
    # 负向断言防误伤：textContent = '…' 的 "ontent" 不是 on* 属性
    bad = re.findall(r"(?<![a-zA-Z])on[a-z]{3,20}\s*=\s*[\"']", src)
    assert not bad, f"发现内联事件 handler 痕迹：{bad[:3]}"


def test_no_comment_terminator_footgun():
    """2026-08-23 实锤：头注释写 `--tk-*/--p`，`*/` 把块注释提前终结 →
    整文件 SyntaxError → 模块静默哑火（加载器防御所以页面无恙但功能没了）。
    钉死：文件里不得出现 `-*/`（CSS 变量名后接注释终结符的唯一坑形）。"""
    assert "-*/" not in _src(), "块注释被 `-*/` 提前终结（模块会整体语法错误）"


def test_builtin_dict_zh_en_parity():
    src = _src()
    used = set(re.findall(r"\bt\('([a-z_0-9]+)'\)", src))
    assert used, "未发现任何 t('key') 用法（词典机制疑似被移除）"
    for key in sorted(used):
        defs = re.findall(rf"^\s+{key}:\s", src, flags=re.M)
        assert len(defs) >= 2, (
            f"词典键 {key!r} 定义次数 {len(defs)} < 2——zh/en 必须各有一份"
        )


def test_zero_symbol_dependency_on_ball_module():
    src = _src()
    # 只许 DOM 协作（querySelector '.asb-*'），不许直接调用球的 JS 符号
    assert "AssistantBall." not in src.replace("window.AssistantBall", ""), (
        "出现对 AssistantBall 内部符号的直接调用——违反双线解耦契约"
    )


def test_ask_does_not_rely_on_ball_click():
    """球开合走 pointerup，HTMLElement.click() 打不开面板。
    2026-08-23 实录：「让小智详细讲讲」只调了 ball.click() → 气泡关了、
    对话不出现。投问必须走 AssistantBall.ask / asb-ask，并先停教学态。"""
    src = _src()
    assert "ball.click()" not in src, (
        "askAssistant 又在对 .asb-ball 调 click()——那是空操作，面板不会开"
    )
    assert "api.ask" in src, (
        "askAssistant 未走 window.AssistantBall.ask 公共入口"
    )
    assert "asb-ask" in src, (
        "缺 asb-ask 事件回落（旧球缓存无 ask() 时的协作词汇）"
    )
    # 先 stop() 再投问：捕获拦截器还在会把开面板当成「再点一次页面」
    ask_fn = src.split("function askAssistant", 1)[-1].split("function ", 1)[0]
    assert "stop()" in ask_fn, "askAssistant 必须先退出教学态再投问"


def test_show_bubble_assigns_s_bubble():
    """2026-08-23：showBubble 建了节点却没写入 S.bubble → closeBubble
    永远空转，「知道了 / 详细讲讲 / Esc」都关不掉气泡。"""
    src = _src()
    assert "S.bubble = bub" in src, (
        "showBubble 必须把新建节点赋给 S.bubble，否则 closeBubble 卸不掉"
    )


def test_loader_init_shell_matches_template():
    for tpl, shell in zip(TEMPLATES, ("admin", "workspace")):
        text = tpl.read_text(encoding="utf-8")
        block = re.search(
            r"assistant-teach\.js\?v=[0-9a-z]+.*?XZTeach\.init\(\{.*?shell:'([a-z]+)'",
            text,
            flags=re.S,
        )
        assert block, f"{tpl.name} 缺 XZTeach.init 调用"
        assert block.group(1) == shell, (
            f"{tpl.name} 的 shell 应为 {shell!r}，实际 {block.group(1)!r}"
        )
