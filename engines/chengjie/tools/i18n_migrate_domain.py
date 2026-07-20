# -*- coding: utf-8 -*-
"""按域把存量 i18n 词条从 web_i18n.py 单体迁移到 i18n_packs(P5 词条治理)。

用法(在 engines/chengjie 目录下):
    python tools/i18n_migrate_domain.py --prefixes msg_ --pack messenger_page [--dry-run]
    python tools/i18n_migrate_domain.py --prefixes tg_s,tg_js_ --pack telegram_page

安全设计:
- 只迁移「单行完整」的词条行(``"key": "value",``);值跨行/续行的键自动跳过并报告,
  留在单体里(避免行级删除破坏语法)。
- 键必须 zh/en 双侧齐平才迁;单侧缺失的键跳过并报告。
- 目标 pack 键与现有全部 pack 无冲突(冲突直接中止)。
- 迁移后自动验证:单体 py_compile 通过 + 子进程重新加载合并视图,
  逐键断言「迁移前后合并视图完全一致」(键集与值都不变),不一致即回滚。
- 单体中在每个语言块首个删除位留一行指路注释。
"""
from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONO = ROOT / "src" / "web" / "web_i18n.py"
PACK_DIR = ROOT / "src" / "web" / "i18n_packs"

_ENTRY_RE = re.compile(r'^( {8})"(?P<key>[^"]+)":\s*(?P<val>"(?:[^"\\]|\\.)*"),\s*(#.*)?$')


def _scan(lines):
    """返回 (zh_entries, en_entries, zh_range, en_range);entry = {key: (line_no, val_literal)}。"""
    zh, en = {}, {}
    state = None
    zh_range = [None, None]
    en_range = [None, None]
    depth = 0
    for i, line in enumerate(lines):
        if state is None and line.strip() == '"zh": {':
            state = "zh"
            zh_range[0] = i
            depth = 1
            continue
        if state == "between" and line.strip() == '"en": {':
            state = "en"
            en_range[0] = i
            depth = 1
            continue
        if state in ("zh", "en"):
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                if state == "zh":
                    zh_range[1] = i
                    state = "between"
                else:
                    en_range[1] = i
                    state = "done"
                continue
            m = _ENTRY_RE.match(line)
            if m and depth == 1:
                (zh if state == "zh" else en)[m.group("key")] = (i, m.group("val"))
    if state != "done":
        raise SystemExit("解析失败:未找到完整的 zh/en 块")
    return zh, en, tuple(zh_range), tuple(en_range)


def _merged_snapshot() -> str:
    """子进程加载合并视图并输出 key=value 快照(避开本进程模块缓存)。"""
    import os
    code = (
        "import sys, json; sys.path.insert(0, '.');"
        "from src.web.web_i18n import get_translations;"
        "print(json.dumps({'zh': get_translations('zh'), 'en': get_translations('en')},"
        " ensure_ascii=False, sort_keys=True))"
    )
    env = dict(os.environ, PYTHONIOENCODING="utf-8")  # Windows 控制台 GBK 防护
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", cwd=str(ROOT), env=env)
    if r.returncode != 0:
        raise SystemExit(f"合并视图加载失败:\n{r.stderr[-2000:]}")
    return r.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefixes", required=True, help="逗号分隔的 key 前缀,如 msg_ 或 tg_s,tg_js_")
    ap.add_argument("--pack", required=True, help="目标 pack 模块名(不带 .py)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    prefixes = tuple(p for p in args.prefixes.split(",") if p)

    pack_path = PACK_DIR / f"{args.pack}.py"
    if pack_path.exists():
        raise SystemExit(f"目标 pack 已存在: {pack_path}(每次迁移用新文件,避免覆盖)")

    src = io.open(MONO, encoding="utf-8").read()
    lines = src.splitlines(keepends=True)
    zh, en, _, _ = _scan(lines)

    def want(k: str) -> bool:
        return any(k.startswith(p) for p in prefixes)

    zh_keys = {k for k in zh if want(k)}
    en_keys = {k for k in en if want(k)}
    both = sorted(zh_keys & en_keys)
    skipped = sorted(zh_keys ^ en_keys)
    if skipped:
        print(f"[skip] 单侧存在或值跨行,留在单体({len(skipped)}): {skipped[:10]}…")
    if not both:
        raise SystemExit("没有可迁移的键")

    # pack 冲突检查
    sys.path.insert(0, str(ROOT))
    from src.web.i18n_packs import collect_packs
    pzh, _pen, _pvi = collect_packs()
    clash = sorted(set(both) & set(pzh))
    if clash:
        raise SystemExit(f"与现有 pack 冲突,中止: {clash[:10]}")

    before = _merged_snapshot()

    # 生成 pack 文件(值用单体中的原始字面量,零转义损耗)
    zh_body = "\n".join(f'    "{k}": {zh[k][1]},' for k in both)
    en_body = "\n".join(f'    "{k}": {en[k][1]},' for k in both)
    pack_src = (
        "# -*- coding: utf-8 -*-\n"
        f'"""{args.pack} 域词条(由 tools/i18n_migrate_domain.py 从单体迁移)。结构见包 docstring。"""\n\n'
        f"ZH = {{\n{zh_body}\n}}\n\n"
        f"EN = {{\n{en_body}\n}}\n"
    )

    # 从单体删除对应行(倒序删,行号不漂移),两块各留指路注释
    drop = sorted([zh[k][0] for k in both] + [en[k][0] for k in both], reverse=True)
    first_zh = min(zh[k][0] for k in both)
    first_en = min(en[k][0] for k in both)
    note_zh = f"        # {'/'.join(prefixes)}* → i18n_packs/{args.pack}.py(P5 迁移)\n"
    note_en = f"        # {'/'.join(prefixes)}* → i18n_packs/{args.pack}.py (P5 split)\n"
    new_lines = list(lines)
    for i in drop:
        del new_lines[i]
    # 注释插入:重新计算删除后位置(first 之前被删的行数)
    off_zh = sum(1 for i in drop if i < first_zh)
    off_en = sum(1 for i in drop if i < first_en)
    new_lines.insert(first_zh - off_zh, note_zh)
    new_lines.insert(first_en - off_en + 1, note_en)

    print(f"迁移 {len(both)} 键 → i18n_packs/{args.pack}.py;单体减 {len(drop)} 行")
    if args.dry_run:
        return 0

    io.open(pack_path, "w", encoding="utf-8", newline="\n").write(pack_src)
    io.open(MONO, "w", encoding="utf-8", newline="").write("".join(new_lines))

    # 验证:编译 + 合并视图逐键一致;失败回滚
    try:
        subprocess.run([sys.executable, "-m", "py_compile", str(MONO), str(pack_path)],
                       check=True, capture_output=True)
        after = _merged_snapshot()
        if before != after:
            raise RuntimeError("合并视图前后不一致")
    except Exception as exc:
        io.open(MONO, "w", encoding="utf-8", newline="").write(src)
        pack_path.unlink(missing_ok=True)
        raise SystemExit(f"验证失败已回滚: {exc}")
    print("验证通过:合并视图逐键一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
