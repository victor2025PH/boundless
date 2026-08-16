# -*- coding: utf-8 -*-
"""前端硬编码颜色棘轮门禁（P3 视觉清欠配套，2026-08-14）。

规则：每个文件的「真实颜色字面量」数只许降不许升——新代码用 dashboard.css 的
语义令牌（var(--green-strong) 等），存量按批清欠后把这里的基线改小。

判定口径（两个反直觉的坑都吃过实锤）：
  * ``&#128260;`` 这类 emoji HTML 实体会撞 6 位 hex 正则——必须排除（(?<!&)）；
  * ``#ef444466`` 8 位带 alpha 的色值不能算 6 位命中（(?![0-9a-fA-F])），
    清欠时对它做裸字符串替换会把前 6 位吞成 var(--x)66 的非法值。

l2-dashboard.html 是独立静态页（拿不到 CSS 变量层），字面量属设计允许，
基线只防继续膨胀。jmuxer.min.js 为三方产物不计。
"""
import re
from pathlib import Path

_HOST = Path(__file__).resolve().parents[1] / "src" / "host"
_HEX_RE = re.compile(r"(?<!&)#[0-9a-fA-F]{6}(?![0-9a-fA-F])")

# 基线（2026-08-15 P4 第二批清欠后实测，scripts/ops/color_token_sweep.py --counts 产出；只许改小。
# 剩余多为 JS 取值位的图表/调色板色（引号紧贴，var() 进 canvas 会画黑，须走 getComputedStyle 方案）
BASELINE = {
    "dashboard.py": 31,
    "dashboard_parts/sidebar.py": 0,
    "static/l2-dashboard.html": 290,
    "static/js/account-farming.js": 19,
    "static/js/alerts-notify.js": 11,
    "static/js/analytics.js": 35,
    "static/js/batch-ops.js": 12,
    "static/js/cluster-ops.js": 3,
    "static/js/conversations.js": 31,
    "static/js/core.js": 11,  # 2026-08-16 P2 清欠：角色徽章 4 色换语义令牌(顺带修日间主题对比)；剩 11 = _WCP worker 调色板 8 + 兜底 3（JS 取值位，var() 进不了取值上下文，设计允许）
    "static/js/device-mgmt.js": 4,
    "static/js/devices.js": 28,
    "static/js/facebook-ops.js": 133,
    "static/js/grid-control.js": 4,
    "static/js/lead-mesh-ui.js": 152,
    "static/js/macros.js": 0,
    "static/js/messages.js": 4,
    "static/js/overview.js": 522,
    "static/js/platform-grid.js": 46,
    "static/js/platform-shell.js": 20,
    "static/js/platforms.js": 175,
    "static/js/router-manage.js": 22,
    "static/js/scripts-templates.js": 3,
    "static/js/studio.js": 117,
    "static/js/system.js": 39,
    "static/js/tasks-chat.js": 39,
    "static/js/tiktok-ops.js": 38,
    "static/js/video-stream.js": 1,
    "static/js/vpn-manage.js": 21,
    "static/js/workflows.js": 23,
}


def _count(rel: str) -> int:
    return len(_HEX_RE.findall((_HOST / rel).read_text(encoding="utf-8")))


def test_color_literals_ratchet():
    """任何文件的颜色字面量数不得超过基线（棘轮只紧不松）。"""
    over = []
    for rel, limit in BASELINE.items():
        got = _count(rel)
        if got > limit:
            over.append(f"{rel}: {got} > 基线 {limit}（新代码请用 dashboard.css 语义令牌）")
    assert not over, "颜色字面量回潮:\n" + "\n".join(over)


def test_baseline_files_exist():
    """基线里的文件必须都在（防改名后棘轮空转）。"""
    missing = [rel for rel in BASELINE if not (_HOST / rel).exists()]
    assert not missing, f"基线文件缺失: {missing}"


def test_new_js_files_registered():
    """新增前端 JS 必须登记进基线（否则不受棘轮保护）。"""
    known = {rel.split("/")[-1] for rel in BASELINE if rel.startswith("static/js/")}
    known.add("jmuxer.min.js")  # 三方产物豁免
    actual = {p.name for p in (_HOST / "static" / "js").glob("*.js")}
    unregistered = sorted(actual - known)
    assert not unregistered, f"新 JS 文件未登记颜色棘轮基线: {unregistered}"
