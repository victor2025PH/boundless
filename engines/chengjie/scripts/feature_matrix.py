# -*- coding: utf-8 -*-
"""功能矩阵导出：feature_registry × FEATURE_MIN_PLAN → 市场/销售可用的 Markdown。

「演示什么 = 交付什么 = 卖什么」三口径同源的最后一块：对外功能矩阵不再手写
（手写=必然漂移），从交付注册表渲染生成——功能名/说明复用功能总览的 i18n 词条，
档位读 licensing 的 FEATURE_MIN_PLAN，三个消费面（设置页/门禁/物料）一个源头。

用法：
    python -m scripts.feature_matrix                        # 打印到 stdout
    python -m scripts.feature_matrix --out docs/功能矩阵_ChatX.md

同步纪律：``docs/功能矩阵_ChatX.md`` 由门禁 ``tests/test_feature_matrix.py``
钉住与注册表逐字一致——改注册表后重跑上面第二条命令，别手改生成物。
刻意不带生成时间戳：内容只由注册表与词条决定，重复生成字节一致
（带时间戳会让同步门禁被噪声逼疯）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.licensing.feature_gate import FEATURE_MIN_PLAN  # noqa: E402
from src.utils.feature_registry import FEATURES, ui_features  # noqa: E402
from src.web.web_i18n import get_translations  # noqa: E402

PLAN_LABEL_ZH = {
    "community": "社区版",
    "basic": "基础版",
    "pro": "专业版",
    "flagship": "旗舰版",
}

_DOC_DEFAULT = Path(__file__).resolve().parent.parent / "docs" / "功能矩阵_ChatX.md"


def _md_escape(s: str) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def render_matrix(lang: str = "zh") -> str:
    t = get_translations(lang)

    def name(f) -> str:
        return t.get(f"fc_f_{f.slug}", f.slug)

    def desc(f) -> str:
        return t.get(f"fc_f_{f.slug}_d", "")

    def plan_cell(f) -> str:
        if f.cls == "A":
            return "全档位（标配）"
        if f.gate_feature:
            plan = FEATURE_MIN_PLAN.get(f.gate_feature, "")
            if plan:
                return f"{PLAN_LABEL_ZH.get(plan, plan)}及以上"
        return "全档位（暂未分档）"

    def note_cell(f) -> str:
        if f.cls == "A":
            return "开箱即用"
        if f.cls == "B":
            if f.requires:
                deps = "、".join(t.get(f"fc_dep_{c}", c) for c in f.requires)
                return f"需配置：{deps}"
            return "设置页一键开启"
        return t.get(f"fc_rsn_{f.reason}", f.reason)

    def table(rows) -> list:
        out = ["| 功能 | 说明 | 档位 | 备注 |", "|---|---|---|---|"]
        for f in rows:
            out.append(
                f"| {_md_escape(name(f))} | {_md_escape(desc(f))} "
                f"| {_md_escape(plan_cell(f))} | {_md_escape(note_cell(f))} |")
        return out

    ui = list(ui_features())
    a_rows = [f for f in ui if f.cls == "A"]
    b_rows = [f for f in ui if f.cls == "B"]
    c_rows = [f for f in ui if f.cls == "C"]
    hidden = len([f for f in FEATURES if not f.show])

    lines = [
        "# ChatX 功能矩阵",
        "",
        "> 本文件由 `python -m scripts.feature_matrix --out docs/功能矩阵_ChatX.md`",
        "> 自动生成，**不要手改**（门禁 `tests/test_feature_matrix.py` 钉住与",
        "> 交付注册表逐字一致）。功能事实源 = `src/utils/feature_registry.py`；",
        "> 档位事实源 = `src/licensing/feature_gate.py::FEATURE_MIN_PLAN`。",
        "",
        "## 基线功能（安装即可用）",
        "",
        *table(a_rows),
        "",
        "## 可解锁功能（设置 → 功能总览 自助开启）",
        "",
        *table(b_rows),
        "",
        "## 增值 / 后续开放",
        "",
        *table(c_rows),
        "",
        f"> 另有 {hidden} 项内部部署开关不在对外矩阵中列出（种子门禁仍全量覆盖）。",
        "> 档位墙仅在授权闸门（`licensing.feature_gate.enabled`）开启的部署生效；",
        "> 内测阶段闸门默认关 = 全功能放行。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="导出功能矩阵 Markdown")
    ap.add_argument("--out", default="", help="输出文件路径（缺省打印 stdout）")
    ap.add_argument("--lang", default="zh", help="词条语言（默认 zh）")
    args = ap.parse_args()
    text = render_matrix(args.lang)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
        print(f"已写入 {out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
