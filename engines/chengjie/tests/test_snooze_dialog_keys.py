# -*- coding: utf-8 -*-
"""搁置弹窗键盘隔离门禁（2026-08-20 内测 B18 实锤）。

事故形态：坐席点「稍后处理」，弹窗开着，按数字想选档位，**字符落进了右栏语音输入框**。

根因不是没拦快捷键——`onKey` 一进来就 `e.stopPropagation()`，全局快捷键确实被隔离了。
但 `stopPropagation` 只切断事件传播，**不取消浏览器的默认插入行为**：默认行为看的是
`defaultPrevented` + 当前焦点元素。而 preventDefault 只写在两个分支里（映射内数字、永久档
数字），于是：

  * 映射外的数字（傍晚只有 3 个预设档 + 永久档=4，按 5 就没人管）
  * 任何普通字符键

在焦点被弹窗外元素（右栏语音框 / 回复框 / 异步渲染抢焦点的组件）持有时，会原样打进那个
输入框。弹窗开着时键盘必须完全属于弹窗，故 `onKey` 里要有「target 不在卡片内 → 吞键 +
把焦点收回卡片」这道兜底。

本门禁是静态 ratchet：真浏览器行为由临时探针实测过（对照组：无弹窗时同样按键确实落字），
这里只钉住那道兜底不被后人顺手删掉。
"""
from __future__ import annotations

import re
from pathlib import Path

TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "unified_inbox.html"


def _snooze_onkey_body() -> str:
    src = TPL.read_text(encoding="utf-8")
    # 同名 onKey 在本文件里有多个（焦点陷阱/其他弹窗）→ 必须从 _snoozeMenu 起点往后找。
    start = src.find("function _snoozeMenu(")
    assert start > 0, "未能定位 _snoozeMenu"
    m = re.compile(r"function onKey\(e\)\{(.*?)\n    \}\n", re.S).search(src, start)
    assert m, "未能在 _snoozeMenu 内定位 onKey 处理器"
    return m.group(1)


def test_snooze_dialog_swallows_keys_when_focus_escaped():
    body = _snooze_onkey_body()
    # ① 焦点逃出卡片时必须整键吞掉（否则字符打进右栏语音框/回复框）
    assert "card.contains(e.target)" in body, (
        "搁置弹窗 onKey 缺少「焦点在卡片外」兜底：stopPropagation 拦不住默认插入行为，"
        "按键会落进当时聚焦的输入框（B18）"
    )
    guard = body.split("card.contains(e.target)", 1)[1][:400]
    assert "preventDefault" in guard, "焦点逃出分支必须 preventDefault，否则字符照样落字"
    assert ".focus()" in guard, "焦点逃出分支应把焦点收回弹窗，否则下一次按键还会落到外面"

    # ② 兜底必须排在数字映射之前——否则「焦点在外」时数字会先被当作选档执行，
    #    等于一次游离按键顺手把会话搁置掉。
    i_guard = body.index("card.contains(e.target)")
    i_digit = body.index("presets.length")
    assert i_guard < i_digit, "「焦点在卡片外」兜底必须排在数字档位映射之前"

    # ③ Escape 仍要能关（兜底不得把逃逸键一律吞成无法退出）
    i_esc = body.index("'Escape'")
    assert i_esc < i_guard, "Escape 分支必须早于焦点兜底，否则焦点在外时按 Esc 关不掉弹窗"


def test_snooze_dialog_grabs_focus_on_open():
    src = TPL.read_text(encoding="utf-8")
    assert re.search(r"card\.querySelector\('\.snz-chip'\);\s*if\(fc\)\s*fc\.focus\(\)", src), (
        "搁置弹窗打开时必须把焦点抢到首个档位按钮上（键盘选档 + 不给外部输入框留焦点）"
    )
