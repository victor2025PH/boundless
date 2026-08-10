# -*- coding: utf-8 -*-
"""渠道中心页「渲染完整性」门禁（2026-07-29 事故回归网）。

背景：模板层的吞噬类事故（如 Jinja 注释陷阱把 ``{% endblock %}`` 连同页头
一起吃掉）在 Jinja 语法检查、纯文本契约门禁下全部为绿，只有**真渲染**才暴露
——当时的症状：正文被渲染进 <head>、顶栏跑到正文之后、<main> 空置、div 失配。

本门禁对四个渠道页做已登录真渲染，钉三条结构不变量：
1. div 开/闭配平（吞噬必然失配）；
2. 壳序：顶栏（ws-top）在正文标记（chc-wrap 的 <div>，非 CSS 文本）之前；
3. 正文在 <main class="ws-body"> 之内（main 在正文标记之前且非空）。
"""
from __future__ import annotations

import re

import pytest

_CHANNELS = ("telegram", "line", "messenger", "whatsapp")


@pytest.mark.parametrize("channel", _CHANNELS)
def test_channel_page_shell_integrity(auth_client, channel):
    r = auth_client.get(f"/workspace/channels/{channel}")
    assert r.status_code == 200, (channel, r.status_code)
    html = r.text

    # 1) div 配平：吞噬/半截标签必然失配
    opens = len(re.findall(r"<div\b", html))
    closes = html.count("</div>")
    assert opens == closes, f"{channel}: div 失配 open={opens} close={closes}"

    # 2) 壳序：顶栏必须渲染在正文标记之前（用带 < 的标记串避免命中 CSS 文本）
    i_top = html.find('class="ws-top')
    i_body = html.find('<div class="chc-wrap"')
    assert i_top >= 0, f"{channel}: 工作台顶栏缺失"
    assert i_body >= 0, f"{channel}: 渠道正文缺失"
    assert i_top < i_body, f"{channel}: 正文渲染在顶栏之前（内容疑似进了 <head>）"

    # 3) 正文必须落在 <main class="ws-body"> 内
    #    class 列表按前缀匹配：壳层会按页附加修饰类（如开了左侧导航的
    #    ws-has-side），钉死完整 class 串会把「加个修饰类」误报成结构事故。
    i_main = html.find('<main class="ws-body')
    assert i_main >= 0, f"{channel}: <main class=\"ws-body\"> 缺失"
    assert i_main < i_body, f"{channel}: 正文在 <main> 之外"

    # 平台连接卡在场（P2 契约的渲染层复核）
    assert 'id="chc-acct-rail"' in html, f"{channel}: 平台连接卡缺失"
