# -*- coding: utf-8 -*-
"""全站模板 Jinja 编译冒烟门禁（2026-08-17 `){#` 事故沉淀）。

模板热更新＝保存即生产：任何一个模板编译失败，对应页面立刻全员 500
（unified_inbox.html＝整个坐席工作台）。渲染类门禁只覆盖登记页且分钟级；
本门禁把「全部模板可编译」钉成秒级不变量——谁存了半个 Jinja 记号
（CSS ``){#`` 被读成注释起始符 / 未闭合 ``{% %}``）先在这里红，而不是
等坐席撞 500。扫描核心复用 ``tools/template_compile_check``（单一事实源，
CLI 手跑与门禁同口径）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.template_compile_check import (  # noqa: E402
    TEMPLATE_DIR,
    compile_errors,
    iter_template_files,
)


def test_all_templates_compile():
    errs = compile_errors()
    assert not errs, (
        "以下模板 Jinja 编译失败——模板热更新下对应页面**此刻就是 500**，先修再落盘：\n"
        + "\n".join(f"  {e['file']}:{e['line']}: {e['message']}" for e in errs)
        + "\n（高发形态：CSS `){#id` 被 Jinja 读成注释起始符——开括号后加空格拆开；"
        "快速自查：python tools/template_compile_check.py）"
    )


def test_scan_covers_workspace_templates():
    """扫描面自证：核心页必须在列——目录解析漂移/枚举回归时先红这里，而不是静默扫空装绿。"""
    names = {p.name for p in iter_template_files()}
    for must in ("unified_inbox.html", "workspace_base.html", "base.html"):
        assert must in names, f"扫描未覆盖 {must}（TEMPLATE_DIR 解析漂移？当前={TEMPLATE_DIR}）"
    assert len(names) >= 40, f"模板数异常偏低（{len(names)}），扫描面疑似缩水"


def test_detector_catches_comment_bomb(tmp_path):
    """探测器有效性自证：8-17 事故的最小复现必须被抓到（防门禁摆设化）。"""
    (tmp_path / "boom.html").write_text(
        "<style>@media (prefers-reduced-motion: reduce){#x{animation:none;}}</style>",
        encoding="utf-8",
    )
    errs = compile_errors(tmp_path)
    assert errs and errs[0]["file"] == "boom.html", "已知编译炸弹未被抓到——门禁失效"
    assert errs[0]["line"] >= 1


def test_detector_catches_unclosed_block(tmp_path):
    (tmp_path / "half.html").write_text("{% if x %}<b>never closed</b>", encoding="utf-8")
    errs = compile_errors(tmp_path)
    assert errs and errs[0]["file"] == "half.html"


def test_detector_passes_clean_and_fixed_form(tmp_path):
    """反例面：正常 Jinja 注释、事故修复形态（`){ #id`）、普通变量都不得误报。"""
    (tmp_path / "ok.html").write_text(
        "{# fine #}<style>@media (x){ #y{color:red;} }</style><b>{{ v }}</b>{% if a %}1{% endif %}",
        encoding="utf-8",
    )
    assert compile_errors(tmp_path) == []
