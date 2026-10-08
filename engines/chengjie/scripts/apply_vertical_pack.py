#!/usr/bin/env python
"""把垂直行业模板包种进知识库，或导出一份人设 YAML（智语 2026-10-08）。

默认 dry-run，不写库、不写人设。内部包（博彩运营商）需要 ``--internal``。
``--persona-out`` 只写调用方指定的路径，拒绝 ``profiles_runtime.yaml``。

    python scripts/apply_vertical_pack.py --list
    python scripts/apply_vertical_pack.py --pack agency --set brand_name=Acme --set assistant_name=Mia
    python scripts/apply_vertical_pack.py --pack agency --db config/kb.db --set brand_name=Acme --apply
    python scripts/apply_vertical_pack.py --pack gambling_operator          # 拒绝，除非 --internal
    python scripts/apply_vertical_pack.py --pack agency --persona-out /tmp/agency.yaml --set brand_name=Acme
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sets(pairs) -> dict:
    out = {}
    for raw in pairs or []:
        if "=" not in str(raw):
            raise SystemExit(f"bad --set (want key=value): {raw}")
        key, value = str(raw).split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"bad --set (empty key): {raw}")
        out[key] = value
    return out


def main(argv=None) -> int:
    from src.utils.vertical_pack import (
        InternalPackHidden,
        UnknownPack,
        VerticalPackError,
        apply_kb_seed,
        assert_persona_out_allowed,
        holding_line,
        list_packs,
        load_pack,
        persona_profile,
    )

    ap = argparse.ArgumentParser(description="垂直模板包：预览或写入 KB / 导出人设")
    ap.add_argument("--list", action="store_true", help="列出当前可见的包")
    ap.add_argument("--pack", default="", help="包 id，如 agency")
    ap.add_argument("--db", default="", help="kb.db 路径；--apply 时必填")
    ap.add_argument("--set", action="append", default=[], help="占位符 key=value，可重复")
    ap.add_argument("--apply", action="store_true", help="写入 KB（默认只预览）")
    ap.add_argument("--internal", action="store_true", help="允许看到并使用内部包")
    ap.add_argument("--persona-out", default="", help="把填好的人设写成这个 YAML，不写 profiles_runtime.yaml")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    include = True if a.internal else None
    values = _sets(a.set)

    if a.list or not a.pack:
        rows = list_packs(include_internal=include)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                print(f"{row['id']}\t{row['edition']}\t{row['name']}")
        return 0

    try:
        pack = load_pack(a.pack, include_internal=include)
    except UnknownPack:
        print("unknown_pack", file=sys.stderr)
        return 2
    except InternalPackHidden:
        print("internal_pack_hidden", file=sys.stderr)
        return 2

    persona_path = ""
    if a.persona_out:
        dest = Path(a.persona_out)
        try:
            assert_persona_out_allowed(dest)
        except VerticalPackError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        import yaml
        dest.parent.mkdir(parents=True, exist_ok=True)
        profile = persona_profile(pack, values)
        dest.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding="utf-8")
        persona_path = str(dest)

    kb = None
    if a.apply or a.db:
        if not a.db:
            print("kb_db_required", file=sys.stderr)
            return 2
        if a.apply:
            from src.utils.kb_store import KnowledgeBaseStore
            kb = KnowledgeBaseStore(Path(a.db))
        else:
            db_path = Path(a.db)
            if db_path.is_file():
                from src.utils.kb_store import KnowledgeBaseStore
                kb = KnowledgeBaseStore(db_path)

    try:
        report = apply_kb_seed(
            kb, pack["id"], values, apply=bool(a.apply), include_internal=include,
        )
    except (UnknownPack, InternalPackHidden) as exc:
        print(exc.code, file=sys.stderr)
        return 2

    report["holding_reply"] = {lang: holding_line(pack, lang, values=values) for lang in ("zh", "en", "tl")}
    report["after_hours_reply"] = {
        lang: holding_line(pack, lang, kind="after_hours_reply", values=values) for lang in ("zh", "en", "tl")
    }
    if persona_path:
        report["persona_out"] = persona_path
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if a.apply and report["added"] == 0 and report["blocked"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
