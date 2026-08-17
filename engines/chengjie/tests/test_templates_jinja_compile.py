# -*- coding: utf-8 -*-
"""全站模板 Jinja 编译门禁（2026-08-17 生产事故沉淀）。

事故：额度横幅样式块里 CSS 媒体查询写成 ``reduce){#budget-banner`` ——
``{#`` 恰是 Jinja 注释起始记号且后文永不闭合 → ``unified_inbox.html``
整个编译失败 → 工作台 500（13:32–13:50 在线事故窗，模板热更新=保存即上
生产，由另一条线止血：开括号后加空格拆记号）。

盲区本质：既有前端门禁（哑按钮/重复 id/孤儿引用/i18n 键）全是**正则静态
扫描**，没有任何一道真的让 Jinja 把模板编译一遍——CSS/JS 里偶然拼出
``{#`` 这类「对浏览器合法、对 Jinja 致命」的记号，只能等热更新上生产才炸。
本门禁用**生产同一个** Environment（``src.web.admin.templates``——自定义
filter 如 ``display_model`` 在模块导入期注册，编译期就校验 filter 存在性，
裸新建 Environment 会对合法模板误报）把全部模板过一遍真实编译器。

刻意不做「``){#`` 高危子串巡检」：首版试过，全是误报——``){%``/``){{``
是合法的 CSS×Jinja 内联写法，而 ``){#`` 命中的是止血批留下的防回归
Jinja 注释本身。精度 > 覆盖（与 conftest「按写入口精确重定向」同哲学）：
真编译器是唯一可靠判官。

约定：只 parse/compile（get_template），不 render——render 需要业务
context，且本事故类别（解析错误）在 get_template 阶段即抛。
"""
from __future__ import annotations

import pathlib

_TPL_DIR = (pathlib.Path(__file__).resolve().parents[1]
            / "src" / "web" / "templates")


def _all_templates():
    return sorted(
        p.relative_to(_TPL_DIR).as_posix()
        for p in _TPL_DIR.rglob("*.html")
    )


def test_template_dir_exists_and_nonempty():
    names = _all_templates()
    assert _TPL_DIR.is_dir() and names, "模板目录缺失或为空——门禁失去对象"
    # 事故主角必须在扫描范围内（防目录结构挪动后门禁静默空转）
    assert "unified_inbox.html" in names
    assert "workspace_base.html" in names


def test_all_templates_compile_under_production_env():
    from src.web.admin import templates as prod_templates

    env = prod_templates.env
    failures = []
    for name in _all_templates():
        try:
            env.get_template(name)
        except Exception as exc:  # noqa: BLE001 —— 逐个收集，一次点名全部
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, (
        "以下模板 Jinja 编译失败（模板热更新=保存即上生产，这些文件此刻"
        "就会把对应页面打成 500）：\n" + "\n".join(failures)
    )
