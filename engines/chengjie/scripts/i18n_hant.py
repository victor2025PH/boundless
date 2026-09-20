# -*- coding: utf-8 -*-
"""繁体中文 (zh_hant) UI 词包生成器（zh_hant P2，2026-08-27）。

与 vi/th/id 的机翻管线（scripts/i18n_mt.py）不同：简→繁是**确定性转换**不是翻译
——OpenCC s2twp（简体→台湾正体+台湾用语短语表）一跑即得全量覆盖，零 API 成本、
零占位符风险（{x} 与 HTML 标签是 ASCII，转换器不碰）。

用法::

    python -m scripts.i18n_hant generate            # 全量转换 → zh_hant_auto.py
    python -m scripts.i18n_hant generate --dry-run  # 只报统计不落盘
    python -m scripts.i18n_hant sample --n 20       # 抽样人读（落 tmp，避免控制台乱码）

依赖：``pip install opencc-python-reimplemented``（纯 Python，仅生成时需要；
生成产物随仓库走，运行时零新依赖）。

生成规则：
- 来源 = zh 合并视图全量（web_i18n 单体 + 全部 packs，与运行时同机制）。
- 排除人工 zh_hant 词包已覆盖键（复核转正后 regen 不复活，与 i18n_mt 同约定）。
- 后处理术语钉 _PINS：s2twp 惯用「臺」，现代软件界面惯用「台」（工作台/平台/后台）。
- 校验：占位符/HTML 标签与 zh 逐键守恒（理论不可能破，防御转换器升级回归）。
- 缺键兜底方向由 i18n_packs.EXTRA_LANG_BASE 钉为 zh（新键在 regen 前显示简体）。

门禁：tests/test_i18n_zh_hant.py（覆盖率 ≥99% + 转换质量抽检 + 底语言=zh）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_PACKS_DIR = _ROOT / "src" / "web" / "i18n_packs"
_LANG = "zh_hant"
_PH_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*[^>]*>|&[a-z]+;")

# 后处理术语钉（s2twp 输出 → 产品用字）。整词替换、顺序执行；新钉往后加。
_PINS = (
    ("臺", "台"),   # 工作臺/平臺/後臺 → 工作台/平台/後台（现代软件界面惯用）
    ("許可權", "權限"),   # s2twp 把「权限」转成「許可權」；台湾软件界面惯用「權限」（用户管理 P3-20）
)

_HEADER = '''# -*- coding: utf-8 -*-
"""繁體中文 (zh_hant) 全量詞條 —— scripts/i18n_hant.py 自動生成，勿手改。

生成: {created} · OpenCC s2twp + 術語釘 · {n} 鍵（源 zh 全量 {total}）
定位: 簡→繁**確定性轉換**產物（非機翻），缺鍵回落簡體（EXTRA_LANG_BASE）。
人工修訂請「轉正」挪進人工詞包（zh_hant_<域>.py），regen 不會復活已轉正鍵。
門禁: tests/test_i18n_zh_hant.py + tests/test_i18n_extra_langs.py。
"""

{var} = {{
'''


def _converter():
    try:
        import opencc
    except ImportError:
        raise SystemExit(
            "缺 OpenCC：pip install opencc-python-reimplemented（仅生成时需要）")
    return opencc.OpenCC("s2twp")


def convert_value(cc, s: str) -> str:
    t = cc.convert(str(s))
    for a, b in _PINS:
        t = t.replace(a, b)
    return t


def _human_covered() -> set:
    from scripts.i18n_mt import _covered_by_human_packs
    return _covered_by_human_packs(_LANG, _PACKS_DIR)


def run_generate(dry_run: bool = False, packs_dir: Path | None = None,
                 zh_view: dict | None = None) -> dict:
    packs_dir = packs_dir or _PACKS_DIR
    if zh_view is None:
        from src.web.web_i18n import get_translations
        zh_view = get_translations("zh")
    cc = _converter()
    human = _human_covered() if packs_dir == _PACKS_DIR else set()

    out: dict = {}
    stats = {"total": len(zh_view), "written": 0, "identical": 0,
             "skip_human": 0, "invalid": 0}
    for k in sorted(zh_view):
        if k in human:
            stats["skip_human"] += 1
            continue
        zh = str(zh_view[k])
        tr = convert_value(cc, zh)
        # 防御校验（转换器不碰 ASCII，理论恒真；防升级回归）
        if (set(_PH_RE.findall(tr)) != set(_PH_RE.findall(zh))
                or sorted(_TAG_RE.findall(tr)) != sorted(_TAG_RE.findall(zh))
                or (zh.strip() and not tr.strip())):
            stats["invalid"] += 1
            continue
        out[k] = tr
        if tr == zh:
            stats["identical"] += 1
    stats["written"] = len(out)

    target = packs_dir / f"{_LANG}_auto.py"
    if dry_run:
        print(f"[i18n_hant] dry-run: {target} 将含 {stats['written']} 键 "
              f"(与简体同形 {stats['identical']}, 人工包排除 {stats['skip_human']}, "
              f"校验拒绝 {stats['invalid']})")
        return stats

    lines = [_HEADER.format(created=time.strftime("%Y-%m-%d %H:%M:%S"),
                            n=stats["written"], total=stats["total"],
                            var=_LANG.upper())]
    prev = None
    for k in sorted(out):
        g = k.split(".", 1)[0]
        if g != prev:
            lines.append(f"    # ── {g} ──\n")
            prev = g
        lines.append(f"    {k!r}: {out[k]!r},\n")
    lines.append("}\n")
    target.write_text("".join(lines), encoding="utf-8")
    print(f"[i18n_hant] 已写 {target}: {stats['written']} 键 "
          f"(同形 {stats['identical']}, 人工包排除 {stats['skip_human']}, "
          f"拒绝 {stats['invalid']})")
    return stats


def run_sample(n: int = 20) -> None:
    from src.web.web_i18n import get_translations
    zh = get_translations("zh")
    cc = _converter()
    import random
    keys = random.sample(sorted(zh), min(n, len(zh)))
    rows = {k: {"zh": zh[k], "zh_hant": convert_value(cc, zh[k])} for k in keys}
    p = _ROOT / "tmp" / "i18n_hant_sample.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[i18n_hant] 抽样 {len(rows)} 键已写 {p}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="全量转换生成 zh_hant_auto.py")
    g.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("sample", help="随机抽样转换对照（人读质检）")
    s.add_argument("--n", type=int, default=20)
    a = ap.parse_args()
    if a.cmd == "generate":
        run_generate(dry_run=a.dry_run)
        return 0
    if a.cmd == "sample":
        run_sample(a.n)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
