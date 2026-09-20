"""全站模板「死类」门禁：class 引用了设计系统里根本不存在的近似类名 → 样式静默不生效。

实锤（2026-08-04）：5 个页面共 24 处 label 写了 `class="f-lb"`（正牌是 base.html 的
`.f-lbl`），标签退化成裸 inline 文本——旁边输入框宽则被挤上一行、窄则同行居左，
同一表单两种标签位置 + `.f-grp` 纵向底边距在 flex-end 行里把控件抬离基线，
就是关怀页「排列错位」的根因。类名拼错**不报错、不变红**，与「哑按钮」（函数定义了
没挂 window）同族的静默失效，存活两个月无人发现。

只列**已实锤**的死类（拼错形 → 正牌），不做「编辑距离」类泛化猜测——泛化会把各页
自有的 marker 类（如 `.f-input`，episodic_memory 的媒体查询以它为钩子）误判进来。
命中面：HTML `class="…"` 的独立 token + JS `className='…'` 赋值（episodic_memory
的筛选组注入工厂曾以第二种形式携带死类）。
"""
import re
from pathlib import Path

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_ALL = sorted(_TPL_DIR.rglob("*.html"))

# 已实锤死类 → 正牌类。新增条目前先确认全站（模板 + static CSS）确无该类定义。
_DEAD_CLASSES = {"f-lb": "f-lbl"}

_CLASS_ATTR = re.compile(r"""\bclass\s*=\s*(['"])([^'"]*)\1""")


def _hits(html: str, dead: str) -> int:
    n = 0
    for m in _CLASS_ATTR.finditer(html):
        if dead in m.group(2).split():
            n += 1
    n += len(re.findall(r"className\s*=\s*['\"]" + re.escape(dead) + r"['\"]", html))
    return n


def test_no_dead_design_system_classes():
    failures = []
    for f in _ALL:
        html = f.read_text(encoding="utf-8")
        for dead, real in _DEAD_CLASSES.items():
            c = _hits(html, dead)
            if c:
                failures.append(f"  {f.name}: {c} 处 class 用了死类 `{dead}`（正牌是 `{real}`）")
    assert not failures, (
        "模板引用了设计系统里不存在的类名（样式静默不生效，标签/控件排版随缘）：\n"
        + "\n".join(failures)
        + "\n修法：改用正牌类名；若确要新造类，先在页内 <style> 或 base.html 给出定义。"
    )
