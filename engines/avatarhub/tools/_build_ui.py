# -*- coding: utf-8 -*-
"""控制台单文件拼装器（2026-07-16 四视角 P2-1：ui.html 拆模块，静态拼装零运行时开销）。

背景：static/ui.html 曾是 8.6 千行手编巨石文件——任何页签改动都在同一文件里挤，回归面大。
拆分后单一真相变为：
    static/ui_src/ui_shell.html      壳（顶栏/侧栏/弹层/非拆分页签…）+ <!--BUILD:TAB id--> 占位行
    static/ui_src/tabs/<id>.html     每个已拆分页签的完整面板片段（含分隔注释，逐字节原样回拼）
本脚本把片段按占位行拼回 **单文件 static/ui.html**（顶部打「生成产物」横幅）——
不引入运行时框架/请求，离线交付与 /ui 服务方式完全不变。

用法：
    python tools/_build_ui.py            # 拼装并写 static/ui.html
    python tools/_build_ui.py --check    # 门禁：ui.html 与源拼装结果逐字节一致才通过
                                         # （手改生成产物 / 改源忘重跑 → 红灯，进 run_all_tests）
exit 0=成功/一致，1=失败/漂移。
"""
import io
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _read(p: Path) -> str:
    """newline='' 原样读（不做换行翻译）——3.10 兼容（Path.read_text 的 newline 参数 3.13 才有）。"""
    with io.open(p, encoding="utf-8", newline="") as f:
        return f.read()


def _write(p: Path, s: str):
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "static" / "ui_src"
SHELL = SRC / "ui_shell.html"
TABS = SRC / "tabs"
OUT = ROOT / "static" / "ui.html"

# 占位行（独占一行，行尾随壳文件 CRLF）
_MARK = re.compile(r"^<!--BUILD:TAB ([a-z_]+)-->\s*$")
# 生成横幅：插在 <!DOCTYPE html> 之后（注释不进 DOM 渲染，像素回归零影响）
_BANNER = ("<!-- ⚠ 生成产物勿手改：源 = static/ui_src/ui_shell.html + static/ui_src/tabs/*.html，"
           "改源后跑 python tools/_build_ui.py（run_all_tests 内 --check 门禁拦手改漂移） -->")


def assemble() -> str:
    if not SHELL.exists():
        raise FileNotFoundError(f"缺 {SHELL}（壳文件）")
    text = _read(SHELL)
    nl = "\r\n" if "\r\n" in text[:2000] else "\n"
    out_lines = []
    used = []
    for line in text.splitlines(keepends=True):
        m = _MARK.match(line.rstrip("\r\n"))
        if not m:
            out_lines.append(line)
            continue
        tab_id = m.group(1)
        frag = TABS / f"{tab_id}.html"
        if not frag.exists():
            raise FileNotFoundError(f"缺片段 {frag}（占位 BUILD:TAB {tab_id}）")
        out_lines.append(_read(frag))
        used.append(tab_id)
    html = "".join(out_lines)
    # 横幅插到首行（<!DOCTYPE html>）之后；已含横幅的壳不重复插
    if _BANNER not in html:
        i = html.find(nl)
        html = html[:i + len(nl)] + _BANNER + nl + html[i + len(nl):]
    # 防呆：全部片段都必须被引用（片段孤儿=改了没接线）
    orphan = [p.stem for p in TABS.glob("*.html") if p.stem not in used]
    if orphan:
        raise RuntimeError(f"片段未被壳引用：{orphan}（ui_shell.html 缺 BUILD:TAB 占位）")
    return html


def main() -> int:
    check = "--check" in sys.argv
    try:
        html = assemble()
    except Exception as e:
        print(f"  [NG] 拼装失败：{e}")
        return 1
    if check:
        cur = _read(OUT) if OUT.exists() else ""
        if cur == html:
            print(f"  [OK] ui.html 与源一致（{len(html)} 字符，无手改漂移）")
            print("通过")
            return 0
        print("  [NG] ui.html 与 ui_src 源拼装结果不一致——")
        print("       改了生成产物请回迁到 static/ui_src/*；改了源请重跑 python tools/_build_ui.py")
        print("失败 1 项")
        return 1
    _write(OUT, html)
    print(f"✓ 已拼装 static/ui.html（{len(html)} 字符）← ui_shell.html + tabs/{{{', '.join(sorted(p.stem for p in TABS.glob('*.html')))}}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
