"""审计：模板 <style> 里「仅靠静态资产全局集」才解析得出的 var() 引用是否真被挂载。

背景：tests/test_input_theme_contrast.py 第三档（继承链解析）刻意把 static/shared 的
css/js 定义视为全局可达（③ 松度）——页面挂哪些 <link>/<script src> 不逐页解析。
本工具补上那一问：对每个「链上解析不出、只有静态资产才有定义」的 token，检查定义它
的静态文件名是否出现在宿主链任一模板文本里（<link>/<script src> 引用）。

三档判定：
  UNLINKED  = 没有任何宿主挂载定义文件 → 运行时照样失效，真 bug；
  PARTIAL   = 部分宿主挂了部分没挂 → 条件性缺陷，逐宿主排查；
  OK-linked = 全部宿主都挂载 → ③ 松度在该处无暴露。

用法：python tools/audit_template_var_links.py [--strict]
  --strict：存在 UNLINKED/PARTIAL 时 exit 1（可接 CI/巡检）。

2026-08-02 首轮基线：全站仅 6 处 static-only 引用（login/setup.html ↔ auth-surface.css
的 --g/--p/--ps），全部 OK-linked，零暴露——③ 松度经实证安全，故未把挂载检查做进门禁
（复杂度与动态 href 假阳风险不值得）。若将来新页面大量走「模板样式引静态 token」模式，
重跑本工具复核。
"""
import argparse
import fnmatch
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "input_theme_gate", ROOT / "tests" / "test_input_theme_contrast.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def audit():
    gate = _load_gate()
    files = gate.load_templates()
    static_tokens = gate.collect_static_tokens()
    eff_chain_only = gate.effective_defined_by_template(files, set())

    def_map = {}
    static_files = list((ROOT / "src/web/static").rglob("*.css")) + list(
        (ROOT / "src/web/static").rglob("*.js")
    )
    shared = ROOT / "shared"
    if shared.exists():
        for ext in ("*.css", "*.html", "*.js"):
            static_files += list(shared.rglob(ext))
    for p in static_files:
        t = p.read_text(encoding="utf-8", errors="ignore")
        for tok in set(gate._VAR_DEF.findall(t)) | set(gate._VAR_SET_PROP.findall(t)):
            def_map.setdefault(tok, set()).add(p)

    names = list(files)
    ext_of, incs = {}, {}
    for name, text in files.items():
        m = gate._EXTENDS_RE.search(text)
        ext_of[name] = m.group(1) if m else None
        resolved = set()
        for im in gate._INCLUDE_TAG_RE.finditer(text):
            pat = gate._include_pattern(im.group(1))
            if not pat:
                continue
            if "*" in pat or "?" in pat:
                resolved |= {n for n in names if fnmatch.fnmatch(n, pat)}
            elif pat in files:
                resolved.add(pat)
        incs[name] = resolved

    chains = {}

    def chain(name):
        if name in chains:
            return chains[name]
        seen, stack = set(), [name]
        while stack:
            n = stack.pop()
            if n in seen or n not in files:
                continue
            seen.add(n)
            if ext_of.get(n):
                stack.append(ext_of[n])
            stack.extend(incs.get(n, ()))
        chains[name] = seen
        return seen

    hosts_of = {n: [h for h in names if n in chain(h)] for n in names}

    findings = []
    for name, text in sorted(files.items()):
        refs = set()
        for m in gate._STYLE_BLOCK.finditer(text):
            refs |= set(gate._VAR_REF_NO_FALLBACK.findall(m.group(1)))
        static_only = {r for r in refs if r not in eff_chain_only[name] and r in static_tokens}
        if not static_only:
            continue
        host_texts = {h: "".join(files[m] for m in chain(h)) for h in hosts_of[name] or [name]}
        for tok in sorted(static_only):
            deffiles = def_map.get(tok, set())
            linked = {h for h, ht in host_texts.items() if any(p.name in ht for p in deffiles)}
            status = (
                "OK-linked" if linked == set(host_texts)
                else "PARTIAL" if linked else "UNLINKED"
            )
            findings.append(
                (status, name, tok, sorted(p.name for p in deffiles)[:3],
                 sorted(set(host_texts) - linked)[:3])
            )
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="有 UNLINKED/PARTIAL 时 exit 1")
    args = ap.parse_args()
    findings = audit()
    bad = 0
    for st in ("UNLINKED", "PARTIAL", "OK-linked"):
        rows = [f for f in findings if f[0] == st]
        print(f"\n=== {st} ({len(rows)}) ===")
        for _, name, tok, defs, badhosts in rows:
            extra = f"  missing-hosts={badhosts}" if badhosts else ""
            print(f"  {name}: {tok}  defined-in={defs}{extra}")
        if st != "OK-linked":
            bad += len(rows)
    if args.strict and bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
