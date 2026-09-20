# -*- coding: utf-8 -*-
"""前端热更面「编码腐蚀」门禁（2026-08-22 事故沉淀，实施56 P1 期间实锤）。

事故：某编辑器一次保存把 ``unified_inbox.html`` 全文件中文经 gb18030 回路存成
乱码（3398 行 + 无效序列连吞下一字节），模板热更新直上生产——坐席界面裸奔乱码、
另一线用旧缓冲「治乱码」又丢掉当日全部增量，恢复耗一条线一小时。

这类腐蚀有确定性指纹，扫描零假阳性：
- 文件必须是合法 UTF-8（strict 解码不抛）；
- 不得出现 PUA 私用区字符（U+E000–F8FF——gb18030 把 utf8 双字节错读进 PUA）；
- 不得出现替换符 U+FFFD 与经典「锟斤拷」。

扫描面＝**保存即上生产**的热更面（模板 / i18n 词条包 / 共享组件 / 静态前端）。
源码里写 ``\\ue57d`` 转义是 ASCII，不受影响；本门禁只抓**裸字符**。
基线＝全零（2026-08-22 恢复后实测），出现任何命中即红——那不是风格问题，
是有人的编辑器在用错误编码保存，立刻停手排查比事后恢复便宜一百倍。
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

#: 热更面扫描根（目录, 后缀集）——保存即生效、腐蚀即上生产的面
_SCAN_ROOTS = (
    (_REPO / "src" / "web" / "templates", (".html",)),
    (_REPO / "src" / "web" / "i18n_packs", (".py",)),
    (_REPO / "shared" / "copilot", (".html", ".js", ".css")),
    (_REPO / "src" / "web" / "static", (".js", ".css", ".html")),
)

#: 运行时数据目录（生产进程持续写入的媒体/头像，非代码资产）——与
#: test_ui_build_freshness / build 打包同一排除口径。
_EXCLUDE_PARTS = {"__pycache__", "protocol_media", "persona_avatars",
                  "node_modules"}


def _scan_files():
    for root, exts in _SCAN_ROOTS:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            if _EXCLUDE_PARTS & set(p.parts):
                continue
            yield p


def _mojibake_hits(text: str):
    hits = []
    for i, ln in enumerate(text.split("\n"), 1):
        if any(0xE000 <= ord(c) <= 0xF8FF for c in ln) or "\ufffd" in ln \
                or "锟斤拷" in ln:
            hits.append(i)
    return hits


def test_scan_surface_nonempty():
    files = list(_scan_files())
    assert len(files) > 100, (
        f"扫描面异常缩水（{len(files)} 个文件）——扫描根被挪动/清空时本门禁"
        "会静默变成摆设，先修扫描面")


def test_hot_surfaces_are_clean_utf8():
    offenders = {}
    for p in _scan_files():
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            offenders[str(p.relative_to(_REPO))] = f"非法 UTF-8 @byte {e.start}"
            continue
        hits = _mojibake_hits(text)
        if hits:
            offenders[str(p.relative_to(_REPO))] = (
                f"乱码指纹 {len(hits)} 行（首现 L{hits[0]}）")
    assert not offenders, (
        "检测到编码腐蚀指纹（PUA/替换符/锟斤拷/非法 UTF-8）——这些面保存即上"
        "生产，先停手检查是谁的编辑器在用 GBK/gb18030 保存，再从干净来源恢复"
        "（方法档案 tools/_recover_unified_inbox_20260822.py）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(offenders.items())))


def test_detector_actually_detects():
    """探测器有效性自证：真实事故样本必须命中（防检测函数被改成恒真）。"""
    corrupted = "<!-- P2 涓婚\ue57d锛歛uto 璺熼殢绯荤粺 -->"
    assert _mojibake_hits(corrupted)
    assert _mojibake_hits("payload \ufffd here")
    assert _mojibake_hits("经典锟斤拷指纹")
    assert not _mojibake_hits("正常中文注释 // normal ascii")


@pytest.mark.parametrize("sample", ["档位/来源码先译人话", "console.log('ok')"])
def test_no_false_positive_on_healthy_content(sample):
    assert not _mojibake_hits(sample)
