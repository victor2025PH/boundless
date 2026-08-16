# -*- coding: utf-8 -*-
"""前端撞名门禁（2026-08-16 P3）——同一 DOM、同一全局命名空间的两类撞名病防线。

背景（三起实锤，同一病理）：dashboard 是全部页面 div 共居一个 DOM、全部 JS 共居
window 的单页应用——
  * ``id="tpl-list"`` 在脚本工房与模板市场重复 → getElementById 永远命中靠前者，
    模板市场页恒空白；
  * ``loadAlertRulesPage`` 在 alerts-notify.js 内重复定义 → 后定义覆盖前定义，
    告警规则列表恒空；
  * ``startRecording``/``_loadAbStats``/``_renderHealthBar`` 跨文件重名 → 后加载
    文件覆盖先加载文件，投屏录制/TikTok A/B 看板/设备页统计条各自静默失效。

两条断言把这类病焊死：新增 id / 顶层函数撞名当场红，不再靠人眼在 8000 行里发现。

诚实边界：JS 运行期 innerHTML 拼出来的动态 id（带 ${} 插值）扫不到；IIFE/闭包内
的函数不在全局命名空间、不算撞名（正则只匹配列首无缩进的顶层定义）。
"""
import re
from pathlib import Path
from collections import Counter, defaultdict

from src.host.dashboard_parts.sidebar import SIDEBAR_HTML

_HOST = Path(__file__).resolve().parents[1] / "src" / "host"
_DASH = (_HOST / "dashboard.py").read_text(encoding="utf-8")

# HTML 注释里常引用历史 id（如撞名修复的注释原样写了旧 id="..."），剥掉再扫
_STRIP_COMMENTS = re.compile(r"<!--.*?-->", re.S)
_ID_RE = re.compile(r'\bid="([A-Za-z][\w-]*)"')
_FN_RE = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)


def test_dom_ids_unique():
    """静态 DOM 里同一 id 只许出现一次（含 sidebar 生成物）。"""
    html = _STRIP_COMMENTS.sub("", _DASH) + _STRIP_COMMENTS.sub("", SIDEBAR_HTML)
    dupes = {k: v for k, v in Counter(_ID_RE.findall(html)).items() if v > 1}
    assert not dupes, (
        f"DOM id 重复（getElementById 只认第一个，后者页面必然静默失效）: {dupes}"
    )


def test_js_global_functions_unique():
    """dashboard 加载的全部 JS 中，顶层函数名全局唯一（跨文件与同文件都算）。"""
    js_files = re.findall(r'src="/static/js/([\w.-]+\.js)\?', _DASH)
    assert len(js_files) >= 20, f"script 标签解析异常，仅 {len(js_files)} 个"
    where: dict[str, list[str]] = defaultdict(list)
    for jf in js_files:
        src = (_HOST / "static" / "js" / jf).read_text(encoding="utf-8")
        for m in _FN_RE.finditer(src):
            where[m.group(1)].append(jf)
    collisions = {k: v for k, v in where.items() if len(v) > 1}
    assert not collisions, (
        "JS 顶层函数重名（后定义/后加载覆盖前者，先者静默失效）:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(collisions.items()))
    )
