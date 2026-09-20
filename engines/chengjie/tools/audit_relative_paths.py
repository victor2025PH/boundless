# -*- coding: utf-8 -*-
"""审计 src/ 里的 CWD 相对路径字面量，按风险分类（只读，随时可跑）。

**为什么需要它**：双实例部署下生产进程的 CWD 是**实例数据根**
（``D:\\chengjie-instances\\<inst>\\data``），而开发机跑测试/脚本时 CWD 多是引擎根。
于是「相对路径」在本地一切正常、只在真实部署才错位——这类缺陷最难本地复现，
2026-07-29 已实锤一次：``account_self_profile`` 把自身头像写进
``<数据根>/src/web/static/...``（web 服务从不挂载那里）→ LINE 账号头像永久裂图，
且因指纹去重不会重下、404 永久固化。

**关键认知：相对路径并非一概有害。** 落点该在哪，取决于那份东西是「数据」还是「代码」：

  A 被 /static 服务的资产  相对 → 写了也不被服务（URL 永久 404）        **真缺陷**
  B 代码根资源（templates/domains/shared/…）  相对 → 生产解析落空       **真缺陷**
  C 数据（logs/、config/*.db、tmp_*）  相对 → 落实例数据根**恰是想要的**  无害
  D 其余                                                              人工判定

用法::

    python tools/audit_relative_paths.py          # 打印分类报告
    python tools/audit_relative_paths.py --strict # A/B 类非空则退出码 1（可接门禁）

已知的**唯一刻意例外**：``voice_prerender.DEFAULT_BASE_DIR = "assets/voices"``
（写入方 CLI 显式按数据根解析、读取方靠引擎 CWID 契约同址；失效是软的且已被
``prerender_coverage`` 观测覆盖）。详见该常量上方注释与
``tests/test_static_asset_paths.py`` 里成对的两条门禁。

配套硬门禁在 ``tests/test_static_asset_paths.py``（A 类全站零容忍）；本工具补的是
B/D 类的**人工审阅面**——它们需要判断「这东西该在代码根还是数据根」，不适合一刀切。
"""
import argparse
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

#: 刻意保留的相对路径（见模块 docstring）——报告里标注而非当缺陷
_INTENTIONAL = {("src/ai/voice_prerender.py", "assets/voices")}

# 看起来像相对路径的字面量：含 / 或以已知目录名开头，且非绝对/非 URL/非格式串
_LOOKS_PATH = re.compile(r"^(?!/|[A-Za-z]:|https?:|//)[\w.\-{}]+/[\w./\-{}*]+$")
_CODE_ROOT_HINT = ("templates/", "domains/", "shared/", "scripts/", "tools/",
                   "src/", "assets/", "docs/", "web/static", "static/")
_DATA_HINT = ("logs/", "config/", "tmp_", "data/", "events/", "cache/")

# 只关心真的被当路径用的字面量：出现在 Path()/open()/os.path.* 的实参里，
# 或赋给名字里含 PATH/DIR/FILE 的常量。
_PATHY_CALLS = {"Path", "open", "PurePath", "PosixPath", "WindowsPath"}


class V(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel = rel
        self.hits = []

    def _add(self, s, node, why):
        self.hits.append((node.lineno, s, why))

    def visit_Call(self, node):
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name in _PATHY_CALLS:
            for a in node.args:
                s = None
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    s = a.value
                elif isinstance(a, ast.JoinedStr):   # f-string
                    s = "".join(
                        p.value if isinstance(p, ast.Constant) else "{}"
                        for p in a.values)
                if s and _LOOKS_PATH.match(s):
                    self._add(s, node, f"{name}()")
        self.generic_visit(node)

    def visit_Assign(self, node):
        for t in node.targets:
            nm = getattr(t, "id", "") or ""
            if not re.search(r"(PATH|DIR|FILE|ROOT)", nm):
                continue
            v = node.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                if _LOOKS_PATH.match(v.value):
                    self._add(v.value, node, f"const {nm}")
        self.generic_visit(node)


#: 桶顺序（报告与门禁共用）；A/B = 真缺陷类，C = 无害，D = 人工判定
RISKY_KINDS = ("A_SERVED_STATIC", "B_CODE_ROOT")
KIND_ORDER = ("A_SERVED_STATIC", "B_CODE_ROOT", "D_unknown", "C_DATA_ok")
KIND_LABEL = {
    "A_SERVED_STATIC": "A. 被服务静态资产（相对=写了不被服务 → 真缺陷）",
    "B_CODE_ROOT": "B. 代码根资源（相对=生产解析落空 → 真缺陷）",
    "D_unknown": "D. 待人工判定",
    "C_DATA_ok": "C. 数据类（落数据根恰是想要的 → 无害）",
}


def classify(rel: str, literal: str) -> str:
    """把一个相对路径字面量归到风险桶（纯函数，门禁与报告共用同一口径）。"""
    low = literal.lower()
    if any(h in low for h in ("web/static", "static/")):
        return "A_SERVED_STATIC"
    if any(low.startswith(h) or f"/{h}" in low for h in _CODE_ROOT_HINT):
        return "B_CODE_ROOT"
    if any(h in low for h in _DATA_HINT):
        return "C_DATA_ok"
    return "D_unknown"


def is_intentional(rel: str, literal: str) -> bool:
    """是否为已登记的刻意例外（登记处即 ``_INTENTIONAL``，改动需同步门禁）。"""
    return (rel, literal) in _INTENTIONAL


def scan(src_dir: Path = SRC) -> dict:
    """扫描 ``src_dir`` 下所有 .py，返回 ``{kind: [(rel, lineno, literal, why)]}``。

    供 ``tests/test_static_asset_paths.py`` 直接消费——分类逻辑单一事实源在此，
    避免「工具报告绿、门禁口径不同」的两套真相。
    """
    buckets: dict = defaultdict(list)
    for f in sorted(Path(src_dir).rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rel = f.relative_to(Path(src_dir).parent).as_posix()
        v = V(rel)
        v.visit(tree)
        for ln, s, why in v.hits:
            buckets[classify(rel, s)].append((rel, ln, s, why))
    return dict(buckets)


def risky_unregistered(buckets: dict | None = None) -> list:
    """A/B 类中**未登记为刻意例外**的条目（门禁断言用；空=干净）。"""
    b = buckets if buckets is not None else scan()
    out = []
    for k in RISKY_KINDS:
        for rel, ln, s, why in b.get(k, []):
            if not is_intentional(rel, s):
                out.append(f"{rel}:{ln}  {s!r}  ({why})  [{k}]")
    return sorted(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CWD 相对路径风险审计（只读）")
    ap.add_argument("--strict", action="store_true",
                    help="A/B 类未登记条目非空则退出码 1，便于接门禁/CI")
    args = ap.parse_args(argv)

    buckets = scan()
    total = 0
    for k in KIND_ORDER:
        rows = buckets.get(k) or []
        total += len(rows)
        print(f"\n=== {KIND_LABEL[k]} — {len(rows)} 处 ===")
        for rel, ln, s, why in rows[:40]:
            mark = " [刻意例外]" if is_intentional(rel, s) else ""
            print(f"  {rel}:{ln}  {s!r}  ({why}){mark}")
        if len(rows) > 40:
            print(f"  … 另 {len(rows) - 40} 处")
    risky = risky_unregistered(buckets)
    print(f"\n总计 {total} 处候选；A/B 类待处置 {len(risky)} 处（刻意例外已排除）")
    return 1 if (args.strict and risky) else 0


if __name__ == "__main__":
    sys.exit(main())
