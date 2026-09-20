# -*- coding: utf-8 -*-
"""模板运行时兜底门禁（2026-08-17 `){#` 事故第三层防线；核心 src/web/template_guard.py）。

不变量：
- 坏保存落盘后，曾成功编译过的模板继续按最后一版好模板出页（served_stale 计数）；
- 修好后立刻切回新版（不粘旧）；
- 冷启动就坏 / 文件不存在 → 照旧抛（fail loud，绝不掩盖坏部署）；
- CRITICAL 日志按模板名 60s 限流；
- 重复安装幂等（不叠包装层）；
- include 子模板坏保存同样被兜（渲染期 get_template 同一入口）。
"""
import logging

import pytest
from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError
from jinja2.exceptions import TemplateNotFound

from src.web.template_guard import install_template_guard

BOMB = "<style>@media (x){#y{a:b;}}</style>"   # 8-17 事故最小复现：`{#` 永不闭合


def _mkenv(tmp_path):
    # cache_size=0：每次 get_template 都走 loader 重新编译——把生产 auto_reload 的
    # 「mtime 变了就重载」压成「每次都重载」，测试不依赖 mtime 粒度即可复现热更新语义。
    return Environment(loader=FileSystemLoader(str(tmp_path)), cache_size=0)


def test_serves_last_good_on_bad_save_then_recovers(tmp_path, caplog):
    (tmp_path / "t.html").write_text("v1:{{ x }}", encoding="utf-8")
    env = _mkenv(tmp_path)
    state = install_template_guard(env)

    assert env.get_template("t.html").render(x=1) == "v1:1"

    (tmp_path / "t.html").write_text(BOMB, encoding="utf-8")   # 坏保存落盘
    with caplog.at_level(logging.CRITICAL, logger="ai_chat_assistant.template_guard"):
        tpl = env.get_template("t.html")
    assert tpl.render(x=2) == "v1:2", "必须供应最后一版好模板，而不是 500"
    assert state["served_stale"] == 1
    assert any("t.html" in r.message for r in caplog.records), "必须有 CRITICAL 喊修"

    (tmp_path / "t.html").write_text("v2:{{ x }}", encoding="utf-8")   # 修好
    assert env.get_template("t.html").render(x=3) == "v2:3", "修好后必须立刻切新版"
    assert state["last_good"]["t.html"].render(x=4) == "v2:4"


def test_cold_broken_still_raises(tmp_path):
    (tmp_path / "bad.html").write_text(BOMB, encoding="utf-8")
    env = _mkenv(tmp_path)
    install_template_guard(env)
    with pytest.raises(TemplateSyntaxError):
        env.get_template("bad.html")


def test_not_found_still_raises(tmp_path):
    env = _mkenv(tmp_path)
    install_template_guard(env)
    with pytest.raises(TemplateNotFound):
        env.get_template("ghost.html")


def test_install_is_idempotent(tmp_path):
    (tmp_path / "t.html").write_text("ok", encoding="utf-8")
    env = _mkenv(tmp_path)
    s1 = install_template_guard(env)
    s2 = install_template_guard(env)
    assert s1 is s2, "重复安装必须返回同一状态，不叠包装层"
    assert env.get_template("t.html").render() == "ok"


def test_critical_log_throttled_per_template(tmp_path, caplog):
    (tmp_path / "t.html").write_text("good", encoding="utf-8")
    env = _mkenv(tmp_path)
    fake = [1000.0]
    state = install_template_guard(env, clock=lambda: fake[0])
    env.get_template("t.html")

    (tmp_path / "t.html").write_text(BOMB, encoding="utf-8")
    with caplog.at_level(logging.CRITICAL, logger="ai_chat_assistant.template_guard"):
        env.get_template("t.html")
        fake[0] += 5          # 60s 窗内：第二次失败不再刷日志
        env.get_template("t.html")
        assert len(caplog.records) == 1
        fake[0] += 61         # 过窗：再喊一次
        env.get_template("t.html")
        assert len(caplog.records) == 2
    assert state["served_stale"] == 3, "限流只管日志，兜底供应每次都算数"


def test_broken_include_child_served_stale(tmp_path):
    (tmp_path / "child.html").write_text("[c1]", encoding="utf-8")
    (tmp_path / "parent.html").write_text("P:{% include 'child.html' %}", encoding="utf-8")
    env = _mkenv(tmp_path)
    install_template_guard(env)
    assert env.get_template("parent.html").render() == "P:[c1]"

    (tmp_path / "child.html").write_text(BOMB, encoding="utf-8")   # 只坏子模板
    # include 在渲染期同样经 get_template → 子模板走 last-good，父页整页不塌
    assert env.get_template("parent.html").render() == "P:[c1]"
