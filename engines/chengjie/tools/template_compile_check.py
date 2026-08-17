# -*- coding: utf-8 -*-
"""全站模板 Jinja 编译冒烟（2026-08-17 `){#` 事故沉淀）。

事故：额度横幅样式块写了 ``@media (...){#budget-banner...}``——``){#`` 被 Jinja
读成**注释起始符**且永不闭合 → ``unified_inbox.html`` 整个编译失败；模板热更新
＝保存即生产，坐席工作台全员 500，事故窗从 13:32 保存持续到门禁扫出（~25 分钟）。

已有的渲染类门禁（sealed-render / window.T 解析）只覆盖登记过的页面、依赖完整
i18n 上下文、分钟级；本工具把「**全部**模板能否通过 Jinja 编译」收成秒级单点：

    python tools/template_compile_check.py        # 全扫；任一模板编译失败即非零退出

门禁 ``tests/test_template_jinja_compile.py`` 复用本模块当单一事实源（已挂
gate_sweep）。改模板前后手跑一次 ≈2 秒，比「保存后等别人 500」便宜四个数量级。

与生产同参：``src/web/admin.py`` 用 Starlette ``Jinja2Templates`` 的**缺省**
Environment（默认定界符、零扩展）——这里同样用裸 ``Environment()``，避免
「测试参数更宽松 → 测试绿、生产炸」的漂移。若将来生产 env 加扩展/改定界符，
必须同步改这里（成对契约，勿单边动）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from jinja2 import Environment, TemplateSyntaxError

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"


def iter_template_files(root: Path | None = None) -> list[Path]:
    base = Path(root) if root is not None else TEMPLATE_DIR
    return sorted(p for p in base.rglob("*.html") if p.is_file())


def compile_errors(root: Path | None = None) -> list[dict]:
    """Parse 每个模板，返回 ``[{file, line, message}]``；空列表＝全站可编译。

    只做 parse（语法层）不做 render：render 需要 i18n/request 上下文且每页各异，
    属 sealed-render 门禁的职责；编译炸弹（未闭合 ``{#``/``{%``、``){#`` 记号撞车）
    在 parse 层就会爆，这层跑得动全站、秒级、零上下文依赖。
    """
    env = Environment()  # 与生产同参：缺省定界符/零扩展（admin.py Jinja2Templates 缺省环境）
    base = Path(root) if root is not None else TEMPLATE_DIR
    errors: list[dict] = []
    for path in iter_template_files(base):
        rel = path.relative_to(base).as_posix()
        try:
            env.parse(path.read_text(encoding="utf-8"), name=rel, filename=str(path))
        except TemplateSyntaxError as exc:
            errors.append({
                "file": rel,
                "line": int(exc.lineno or 0),
                "message": str(exc.message or exc),
            })
        except UnicodeDecodeError as exc:
            # 模板必须是 UTF-8（生产 FileSystemLoader 同编码）；坏编码同样是加载期事故
            errors.append({"file": rel, "line": 0, "message": f"not utf-8: {exc}"})
    return errors


def main() -> int:
    files = iter_template_files()
    errs = compile_errors()
    if not errs:
        print(f"template compile smoke: OK ({len(files)} templates)")
        return 0
    print(f"template compile smoke: {len(errs)} BROKEN template(s) / {len(files)} scanned")
    for e in errs:
        print(f"  {e['file']}:{e['line']}: {e['message']}")
    print("hint: CSS 里 `){#id`/`{{` 这类字符组合会被 Jinja 当模板记号——括号后加空格拆开;")
    print("      模板热更新=保存即生产, 先修再存(参考 unified_inbox.html 额度横幅样式块的注释)。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
