# -*- coding: utf-8 -*-
"""设计令牌一致性门禁（2026-07-16 桌面×网页对齐 P0）。

单一真相 = static/design-tokens.json；本检查确保：
  1) tokens 文件本身合法且关键键齐全；
  2) 网页 brand.css 的对应变量与 tokens 同值（改一边忘另一边 → 门禁红灯）；
  3) 桌面 launcher_theme.py 确实在消费 tokens（存在加载器且状态色/主题走 tokens 派生）。
exit 0=通过 / 1=不一致（进 run_all_tests 套件）。
"""
import io
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent

fails = []


def ok(msg):
    print(f"  [OK] {msg}")


def ng(msg):
    fails.append(msg)
    print(f"  [NG] {msg}")


def css_var(css: str, name: str) -> str:
    m = re.search(rf"{re.escape(name)}\s*:\s*([#0-9a-fA-F]+)\s*;", css)
    return (m.group(1).strip().lower() if m else "")


def main() -> int:
    tokens_p = ROOT / "static" / "design-tokens.json"
    try:
        dt = json.loads(tokens_p.read_text(encoding="utf-8"))
    except Exception as e:
        ng(f"design-tokens.json 不可读/不合法: {e}")
        return 1
    state = dt.get("state") or {}
    for k in ("ok", "warn", "danger", "down"):
        if not str(state.get(k, "")).startswith("#"):
            ng(f"tokens.state.{k} 缺失或非 #RRGGBB")
    for k in ("accent", "accent2"):
        if not str(dt.get(k, "")).startswith("#"):
            ng(f"tokens.{k} 缺失或非 #RRGGBB")
    if not str((dt.get("dark") or {}).get("bg", "")).startswith("#"):
        ng("tokens.dark.bg 缺失")
    if not fails:
        ok("design-tokens.json 结构与关键键齐全")

    css = io.open(ROOT / "static" / "brand.css", encoding="utf-8").read()
    pairs = [("--bd-ok", str(state.get("ok", "")).lower()),
             ("--bd-warn", str(state.get("warn", "")).lower()),
             ("--bd-danger", str(state.get("danger", "")).lower()),
             ("--bd-acc", str(dt.get("accent", "")).lower()),
             ("--bd-acc2", str(dt.get("accent2", "")).lower()),
             ("--bd-bg", str((dt.get("dark") or {}).get("bg", "")).lower())]
    for var, want in pairs:
        got = css_var(css, var)
        if got and want and got == want:
            ok(f"brand.css {var} = tokens ({want})")
        else:
            ng(f"brand.css {var}={got or '未找到'} 与 tokens {want} 不一致")

    lt = io.open(ROOT / "launcher_theme.py", encoding="utf-8").read()
    for needle, label in [("_load_design_tokens", "tokens 加载器"),
                          ('_DT_STATE.get("ok"', "状态色走 tokens 派生"),
                          ('_DT_DARK.get("bg"', "暗色主题走 tokens 派生"),
                          ("ACCENT2", "品牌辅助色(蓝紫渐变)")]:
        if needle in lt:
            ok(f"launcher_theme {label}")
        else:
            ng(f"launcher_theme 缺少 {label}（桌面端未消费 tokens）")

    print(("通过" if not fails else f"失败 {len(fails)} 项"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
