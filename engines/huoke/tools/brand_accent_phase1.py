# -*- coding: utf-8 -*-
"""huoke 后台品牌强调色 Phase-1：--accent 令牌层对齐无界智连蓝（2026-07-30）。

范围（刻意窄）：只动 css/dashboard.css 的 --accent/--accent-glow 定义 + 与交互
态强耦合的少量字面量（hover 深档 / 选中框 / 拖拽 tint / 点击涟漪 / 进度条）。
大多数规则已走 var(--accent)，改定义点即全站翻转。

映射按梯位保真：dark #3b82f6(500)→#1e8cf2(growth-500)；light #2563eb(600)→
#0d76d9(growth-600)；hover 深档 #2563eb→#0d76d9；#60a5fa(400)→#54a7f5(growth-400)。

**刻意保留**（调色板臂/语义色，动了会破坏体系）：stat-card.blue 卡族、
badge-running/ts-running/tb-running 运行态族、log INFO 级别色、toast.info、
anomaly info、funnel-colors-1 序列、oc-dlg info 对话框、JS 里全部状态/阶段/
等级/图表调色板。JS 与 l2-dashboard.html 属 Phase-2（其页面局部强调
#60a5fa ≈ growth-400 视觉近同，优先级低）。

每条替换断言唯一命中——命中数≠1 即中止不落盘，防止误伤相邻保留项。
"""
from __future__ import annotations

import sys
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "src" / "host" / "static" / "css" / "dashboard.css"

# (旧串, 新串) —— 全部要求唯一命中
EDITS = [
    # 令牌定义：dark(默认) 500 阶 / light 600 阶
    ("  --accent: #3b82f6;\n  --accent-glow: rgba(59,130,246,.25);",
     "  --accent: #1e8cf2;\n  --accent-glow: rgba(30,140,242,.25);"),
    ("  --accent: #2563eb;\n  --accent-glow: rgba(37,99,235,.18);",
     "  --accent: #0d76d9;\n  --accent-glow: rgba(13,118,217,.18);"),
    # 品牌 logo 渐变蓝臂随主题令牌
    (".sidebar-logo h1{font-size:18px;font-weight:700;background:linear-gradient(135deg,#3b82f6,#8b5cf6);",
     ".sidebar-logo h1{font-size:18px;font-weight:700;background:linear-gradient(135deg,var(--accent),#8b5cf6);"),
    # 交互 hover 深档（基色是 var(--accent)，hover 是其下一阶）
    (".dev-btn.screen:hover{background:#2563eb}",
     ".dev-btn.screen:hover{background:#0d76d9}"),
    (".chat-input-area button:hover{background:#2563eb}",
     ".chat-input-area button:hover{background:#0d76d9}"),
    (".tk-confirm-btns .btn-exec:hover{background:#2563eb}",
     ".tk-confirm-btns .btn-exec:hover{background:#0d76d9}"),
    # 拖拽 / 悬停 / 选中 tint（伴随 var(--accent) 出现）
    (".nm-row.drag-over{background:rgba(59,130,246,.12);border:1px dashed var(--accent)}",
     ".nm-row.drag-over{background:rgba(30,140,242,.12);border:1px dashed var(--accent)}"),
    ("padding:8px 16px;background:rgba(59,130,246,.04);",
     "padding:8px 16px;background:rgba(30,140,242,.04);"),
    (".scr-badge{position:absolute;top:8px;left:8px;background:rgba(59,130,246,.85);",
     ".scr-badge{position:absolute;top:8px;left:8px;background:rgba(30,140,242,.85);"),
    (".scr-card.selected{border-color:#3b82f6;box-shadow:0 0 0 2px rgba(59,130,246,.5)}",
     ".scr-card.selected{border-color:var(--accent);box-shadow:0 0 0 2px rgba(30,140,242,.5)}"),
    (".group-toolbar .grp-btn:hover{border-color:var(--accent);background:rgba(59,130,246,.1)}",
     ".group-toolbar .grp-btn:hover{border-color:var(--accent);background:rgba(30,140,242,.1)}"),
    # 点击涟漪 / 轨迹（RPA 可视化标记，与 messenger tap-marker 同语义）
    ("border:2px solid #3b82f6;background:rgba(59,130,246,.25);pointer-events:none;animation:rippleO",
     "border:2px solid var(--accent);background:rgba(30,140,242,.25);pointer-events:none;animation:rippleO"),
    ("background:rgba(59,130,246,.3);width:10px;height:10px;animation:trailFade .4s ease forwards}",
     "background:rgba(30,140,242,.3);width:10px;height:10px;animation:trailFade .4s ease forwards}"),
    # 进度条（独立强调，非状态族）
    (".progress-fill{height:100%;background:linear-gradient(90deg,#3b82f6,#60a5fa);border-radius:3px;transition:width .5s ease}",
     ".progress-fill{height:100%;background:linear-gradient(90deg,#1e8cf2,#54a7f5);border-radius:3px;transition:width .5s ease}"),
    (".bpp-bar{height:100%;width:0%;background:#3b82f6;border-radius:3px;transition:width .8s ease,background .4s}",
     ".bpp-bar{height:100%;width:0%;background:var(--accent);border-radius:3px;transition:width .8s ease,background .4s}"),
    # 高亮选中块（!important 组）
    ("border: 2px solid rgba(59,130,246,.6) !important;",
     "border: 2px solid rgba(30,140,242,.6) !important;"),
    ("0%, 100% { box-shadow: 0 0 4px rgba(59,130,246,.2); }",
     "0%, 100% { box-shadow: 0 0 4px rgba(30,140,242,.2); }"),
    ("50% { box-shadow: 0 0 14px rgba(59,130,246,.35); }",
     "50% { box-shadow: 0 0 14px rgba(30,140,242,.35); }"),
    ("background: #3b82f6 !important;",
     "background: var(--accent) !important;"),
    ("background: rgba(59,130,246,.12) !important;",
     "background: rgba(30,140,242,.12) !important;"),
    ("border-top: 1px solid rgba(59,130,246,.25);",
     "border-top: 1px solid rgba(30,140,242,.25);"),
]


def main() -> int:
    text = CSS.read_text(encoding="utf-8")
    for old, new in EDITS:
        n = text.count(old)
        if n != 1:
            print(f"[中止] 命中数 {n} ≠ 1：{old[:70]!r}")
            return 1
    for old, new in EDITS:
        text = text.replace(old, new)
    CSS.write_text(text, encoding="utf-8")
    print(f"完成：{len(EDITS)} 处（唯一命中校验全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
