# -*- coding: utf-8 -*-
"""内联硬编码颜色 ratchet 门禁（P2-3 建账，P3-1 收口升级，2026-07-23）。

问题：`style="...#7f1d1d..."` 这类**内联 style 属性里的硬编码颜色**特异性最高、
无法被主题层（暗色模式 / 品牌白标 tokens）覆盖，是暗色主题收口的核心债务。
base.html 系页面已有真实暗色主题（[data-theme=dark] + OS 偏好自动进入），
内联硬编码色在暗色下不翻转＝现行破损。

P3-1 收口（tools/inline_color_codemod.py）：654 个色值按 (角色, 字面量) 映射成
`var(--th-*, <原字面量>)`——亮色渲染字节级零变化（fallback ≡ :root 亮值），
暗色由 static/theme-tokens.css 的 [data-theme=dark] 块翻转。

口径（刻意窄）：
  - 只数**静态 style 属性**（style="..." / style='...'）里含 #hex 或 rgb()/rgba()
    颜色字面量的属性个数（一个属性算 1，无论里面几个色值）；
  - **var(...) 段整体豁免**——`var(--th-x,#hex)` 的 fallback 是主题化后的兜底值，
    不算硬编码（先剥 var 段再找色值）；
  - `<style>` 块不算——页级 CSS 可以被更晚的主题层覆盖，且集中迁移更合适；
  - JS 动态拼的 el.style.x = '#fff' 不算（掩码/求值成本高，另行治理）；
  - style 属性只有布局值（display/width/gap…）不算——那些与主题无关。

配套不变量（本文件后三个测试）：
  1. theme-tokens.css 必须挂在全部根布局（漏挂＝该系页面暗色不翻转，
     但因 fallback 存在亮色仍正确——软失败，门禁把它变硬）；
  2. 模板里每个 var(--th-x, fallback) 的 fallback ≡ theme-tokens.css :root 亮值
     （颜色等价比较，#fff ≡ #ffffff）——防"改了 CSS 忘了模板"两处漂移；
  3. 模板引用的每个 --th-* token 必须在 theme-tokens.css 有定义（防拼错）。

台账维护：继续迁移后把对应文件数字改小；`test_inline_color_ledger_not_stale`
会在实际值低于天花板时点名要求收紧，防止台账虚高吞掉倒退空间。
"""
from __future__ import annotations

import pathlib
import re

_REPO = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES_ROOT = _REPO / "src/web/templates"
THEME_CSS = _REPO / "src/web/static/theme-tokens.css"

# 前置词界 `(?<![\w-])` 是必需的，不是防御性写法：没有它，`re.IGNORECASE` 会让
# **JS canvas 的 `g.fillStyle = '#fcd34d'` / `strokeStyle = 'rgba(...)'` 整片命中**
# （"fillStyle" 的尾巴就是 "style"）。2026-08-22 实锤：membership.html 的邀请海报
# 渲染器（canvas 逐行画字）被记成 11 处「内联硬编码色」，是当时台账上最大的一笔，
# 而该文件真实计数是 0。后果不只是数字虚高——它让门禁**指着一个没做错事的文件**，
# 于是每条来看的线都在 membership.html 里翻到一堆 canvas 代码、合理地判断「这跟内联
# style 无关，不是我的事」然后走开；红灯因此在共享账本上挂了 76 小时无人认领，把
# 另外三个**真**违规一起埋了。canvas 绘图色本就在本门禁射程外（见上方口径第 4 条：
# JS 动态色另行治理），所以这是纯假阳性，修掉零成本。
_STYLE_ATTR = re.compile(
    r"""(?<![\w-])style\s*=\s*("([^"]*)"|'([^']*)')""", re.IGNORECASE)
_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")

# ── 账本：文件 → 含硬编码颜色的内联 style 属性数天花板（只许降不许升） ──
# 2026-07-23 P2-3 基线 638（43 模板）→ P3-1 三批收口后 103（26 模板，-84%）。
# 剩余=映射表外长尾一次性装饰色 + box-shadow 阴影色（阴影主题无关，刻意豁免
# 角色映射但仍计数），后续批次继续收紧。未列出的文件天花板=0。
_INLINE_COLOR_CEILINGS = {
    # 2026-08-06 登记：告警接通共享部件（暖色横幅+弹窗，2026-08-05 alertlink 线新建，
    # 未提交在途设计）——横幅刻意用固定暖棕/奶油色（跨主题恒定的告警视觉），
    # token 化归属其 owner 线；先登记保 sweep 绿 + 债务可见（台账语义与 _PENDING_* 同）。
    # 2026-08-27 实施75 batch2：未接通横幅（暖棕渐变+奶油字）随顶部退役摘除，6→3。
    "_alertlink_connect.html": 3,
    # 2026-08-18 暗色适配整页 pass 后收紧 6→2（goal-push 线）：剩 2 处=图表系列
    # 常量（趋势图「赢单」金 #ca8a04 图例圆点×1 + 热力格 JS 拼 rgba 渐变、深格
    # 白字×1）——数据可视化系列色两主题恒定属刻意常量（与 SVG stroke 同色一体），
    # 其余弹层说明/轴标/图例灰全部 token 化，卡面/表格/徽章/弹层随主题。
    "goal_report.html": 2,
    # 2026-08-10 登记（清账线代记，owner=cases 线，意向 16h 前已收尾）：1 处，
    # token 化归属 owner；数值取自门禁实测。
    "cases.html": 1,
    "_rpa_shared_scripts.html": 4,
    # agent_perf.html：2026-08-18 暗色令牌归队批清零（dashed 分隔线/force_override
    # 紫字/cooldown chip 全部 token 化），除名。
    "ai_studio.html": 2,
    # 2026-08-22 1→4 代记（语音可观测线代记，owner=后台壳陈旧页横幅线 2026-08-21）：
    # +3 处全在 `#adm-uibuild` 横幅（深青渐变 #134e4a→#0f766e 底 + 常量浅青字
    # #ccfbf1 + 按钮白透明描边/底）——与 workspace_base 台账里「维护窗/信息横幅
    # 渐变端点色（深底常量，精确 token 不在映射表）」同判据同处置：深底横幅两主题
    # 恒定，token 化需先扩 codemod 映射表单源，归后续暗色收口批。
    # 2026-08-27 实施75 batch2：adm-uibuild 顶部横幅（深青渐变+浅青字）随迁移摘除，4→1。
    # 2026-08-31 新手引导退役：最后一处内联色随 tour/onboard-modal 删除清零，1→0。
    "base.html": 0,
    # 2026-08-22 1→2 代记（同上，owner=styleguide 线）：+1 处 `acc-hd-icon`
    # 装饰渐变（#ec4899→#db2777），属本台账「映射表外长尾一次性装饰色」既有类别。
    "developer.html": 2,
    "draft_review.html": 3,
    "knowledge.html": 4,
    # 渠道中心融合：四渠道正文迁 _channel_body_*.html（计数随内容平移）
    "_channel_body_line.html": 1,
    # P2-3（2026-08-02）主 IIFE 外迁 static/messenger/messenger_rpa.js，
    # JS 串里的内联色随迁移出模板 10→8（本门禁只扫模板；JS 侧旧蓝由
    # test_legacy_blue_ratchet 的 JS 条目续管）
    "_channel_body_messenger.html": 8,
    "ops/contacts.html": 1,
    "ops/mobile_handoffs.html": 1,
    "ops_overview.html": 2,
    # personas.html：2026-07-30 品牌收口把最后一个内联硬编码色转成 color-mix(var(--p)) → 0，除名
    # queue_monitor.html：2026-08-18 整页随主题令牌化（老板拍板废弃恒暗大屏语义），
    # 刷新按钮描边/虚拟坐席头像底/负载条轨三处全部 token 化 → 0，除名。
    # relations_health.html：2026-08-01 任务台改版把仅剩内联色恢复 --th-* token → 0，除名
    "rpa_overview.html": 11,
    "settings.html": 9,
    "setup_wizard.html": 1,
    "strategies.html": 2,
    # P3-1（2026-08-02）图标 SVG 化把快照卡内联紫图标盒归一标准色类 → 2→1
    "_channel_body_telegram.html": 1,
    # 2026-08-07 内联 AI 副驾面板下线（遮挡消息原文）随删一处内联色 10→9；
    # 2026-08-10 9→11（清账线按 git diff 归因后代记，owner=接管体检线）：AI 体检
    # 弹层 #ai-diag-overlay 的遮罩 rgba(15,23,42,.45) + 卡片 box-shadow rgba(0,0,0,.25)
    # ——与本台账既有「阴影/遮罩主题无关，刻意豁免角色映射但仍计数」同类；该线
    # .py 尚待重启装载属在途批次，token 化（或换 --th-bg-scrim* 族）归 owner。
    "unified_inbox.html": 11,
    # P3-1（2026-08-02）图标 SVG 化把 7 处内联色图标盒归一标准色类 → 13→8
    "_channel_body_whatsapp.html": 8,
    # workflows.html：2026-08-09 暗色收口（页内 --wf-* 变量 + 双开关暗段）随手清掉
    # 最后一处内联 #f8fafc → 0，除名
    # workspace_base：2026-07-29 抽类后定 4；后加的配额墙弹窗 2×color:#fff 顶爆到 6
    # 红了 24h+ 无人认领。2026-08-12 清账：#fff→--th-ink-inverse（映射表口径）、
    # 搜索框白透明玻璃态→color-mix(var(--th-ink-inverse))；余 3 处刻意留台账＝
    # gs-panel 阴影（box-shadow 主题无关，工具方针豁免映射）+ 维护窗/信息横幅
    # 渐变端点色×2（深底常量，精确 token 不在映射表；token 化需动 codemod 单源，
    # 归属后续暗色收口批）。
    # 2026-08-17 3→4 登记（msgops 线代记，HEAD diff 归因）：+1 横幅动作按钮
    # 白透明描边/底（rgba(255,255,255,.5)，深色渐变横幅上的常量白，与上行
    # 「渐变端点色」同判据；owner=横幅线，token 化随暗色收口批一并）。
    # 2026-08-22 4→7 代记（语音可观测线代记，owner=接待状态 v2 线 2026-08-19）：
    # +3 处＝`ws-pr-dot` 状态点三连（在线绿 #22c55e / 忙碌琥珀 #f59e0b / 离开灰
    # #94a3b8）。**这三处与本台账其余条目不同类，是真正该 token 化的**——它们不是
    # 2026-08-23 appmenu 线清账 7→4：ws-pr-dot 三个接待状态点自内联迁类规则
    # （.ws-pr-dot.on/.busy/.off）。刻意保留字面量未迁 token：宿主 .ws-user-menu
    # 两主题恒白底（「整卡主题化属后续批次」原注），面不翻转则点不跟 token 翻转
    # ——与 .ws-ava-dot 同先例；菜单卡将来主题化时整组一起迁。余 4 处＝gs-panel
    # 阴影（box-shadow 主题无关豁免映射）+ 维护窗/信息横幅渐变端点色×2 等深底
    # 常量（精确 token 不在映射表，token 化需动 codemod 单源，归暗色收口批）。
    # 2026-08-27 实施75 蓝条退役：重启冷却顶部横幅（渐变端点深底常量 #1e3a5f）
    # 随 DOM 摘除，4→3；batch3 通道离线红条与 .stale 琥珀降色 CSS 再摘 2 处，
    # 3→1；余 1 处＝gs-panel 阴影（box-shadow 主题无关，工具方针豁免映射）。
    "workspace_base.html": 1,
    "workspace_dashboard.html": 4,  # 2026-07-30 品牌收口：当前行高亮 → color-mix(var(--tk-brand))
    # workspace_usage.html：2026-07-30 品牌收口把图例点 #93c5fd → var(--bl-growth-300) → 0，除名
}

# theme-tokens.css 必须挂载的根布局（覆盖全部 43 个欠账页的 extends 链）
# ops_overview.html 2026-08-03 P1 挂壳后 extends base.html，token CSS 随壳继承，不再是根。
_TOKEN_ROOTS = (
    "base.html",
    "workspace_base.html",
    "ops/contacts.html",
    "ops/merge_reviews.html",
    "ops/mobile_handoffs.html",
)


def _strip_var_spans(val: str) -> str:
    """把 var(...) 段（含嵌套括号，如 var(--x,rgba(0,0,0,.5))）替换成等长空白。"""
    out = list(val)
    low = val.lower()
    i = 0
    while True:
        j = low.find("var(", i)
        if j < 0:
            break
        depth = 0
        k = j + 3
        while k < len(val):
            if val[k] == "(":
                depth += 1
            elif val[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        end = k + 1 if k < len(val) else len(val)
        for m in range(j, end):
            out[m] = " "
        i = end
    return "".join(out)


def _count_inline_color_styles(text: str) -> int:
    n = 0
    for m in _STYLE_ATTR.finditer(text):
        val = m.group(2) if m.group(2) is not None else m.group(3)
        if _COLOR.search(_strip_var_spans(val or "")):
            n += 1
    return n


def _scan() -> dict:
    counts = {}
    for p in sorted(TEMPLATES_ROOT.rglob("*.html")):
        rel = p.relative_to(TEMPLATES_ROOT).as_posix()
        n = _count_inline_color_styles(p.read_text(encoding="utf-8", errors="replace"))
        if n:
            counts[rel] = n
    return counts


def test_inline_color_not_above_ceiling():
    counts = _scan()
    offenders = []
    for rel, n in counts.items():
        cap = _INLINE_COLOR_CEILINGS.get(rel, 0)
        if n > cap:
            offenders.append(f"{rel}: {n} > 天花板 {cap}")
    assert not offenders, (
        "内联 style 硬编码颜色超出台账天花板——新代码请用主题 token"
        "（var(--th-*,#亮值)，见 static/theme-tokens.css + tools/inline_color_codemod.py），"
        "不要写 style=\"...#hex...\"：\n  " + "\n  ".join(offenders)
    )


def test_scanner_ignores_js_canvas_style_properties():
    """`fillStyle`/`strokeStyle` 不得被当成 style 属性（2026-08-22 假阳性回归钉）。

    钉这条的理由是**假阳性比漏报更贵**：漏一处装饰色，暗色下顶多一个色块不翻转；
    而错点一个无辜文件，会让所有来排查的线得出「这门禁在胡说」的结论，从此整条
    红灯失去可信度——实测代价是 76 小时无人认领 + 三个真违规被一起埋掉。

    同时钉住「真的内联 style 仍然要抓」，否则这条修复可能被写成一刀切的豁免。
    """
    canvas = """<script>
      g.fillStyle = '#fcd34d';
      g.strokeStyle = 'rgba(252,211,77,.65)';
      ctx.shadowColor = "#000";
    </script>"""
    assert _count_inline_color_styles(canvas) == 0

    # 真违规（属性位、含硬编码色）必须仍然计数；var() fallback 仍然豁免
    assert _count_inline_color_styles('<div style="color:#fff">x</div>') == 1
    assert _count_inline_color_styles(
        '<div style="color:var(--th-ink,#111)">x</div>') == 0
    # 词界只挡「style 前面粘着标识符字符」，不挡正常的属性前空白/引号
    assert _count_inline_color_styles("<div id='a'style='color:#fff'>") == 1


def test_inline_color_ledger_not_stale():
    counts = _scan()
    stale = []
    for rel, cap in _INLINE_COLOR_CEILINGS.items():
        actual = counts.get(rel, 0)
        if actual < cap:
            stale.append(f"{rel}: 实际 {actual} < 天花板 {cap}（请把台账收紧到 {actual}）")
    assert not stale, (
        "迁移成果未落进台账（天花板虚高会吞掉倒退空间）：\n  " + "\n  ".join(stale)
    )


# ═══════════════════════ P3-1 主题 token 配套不变量 ═══════════════════════

def _canon_color(lit: str) -> str:
    """颜色等价规范化：小写去空白 + 3/4 位 hex 展开（#fff ≡ #ffffff）
    + rgba alpha 前导零折叠（0.08 ≡ .08）——与 tools/inline_color_codemod.py 同口径。"""
    s = lit.lower().replace(" ", "").replace("\t", "")
    if s.startswith("#") and len(s) in (4, 5):
        s = "#" + "".join(ch * 2 for ch in s[1:])
    return re.sub(r"(?<=[,(])0\.(\d)", r".\1", s)


def _parse_css_block(css: str, header: str) -> dict:
    """从 theme-tokens.css 抽 `header{...}` 块里的 --th-*:value 对。"""
    m = re.search(re.escape(header) + r"\{(.*?)\}", css, re.DOTALL)
    assert m, f"theme-tokens.css 缺 {header} 块"
    out = {}
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(";")
        if line.startswith("--th-") and ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


_VAR_TH = re.compile(r"var\((--th-[a-z0-9-]+)\s*,")


def _iter_template_var_refs():
    """yield (rel, token, fallback)：模板里每个 var(--th-x, fallback) 引用。"""
    for p in sorted(TEMPLATES_ROOT.rglob("*.html")):
        rel = p.relative_to(TEMPLATES_ROOT).as_posix()
        txt = p.read_text(encoding="utf-8", errors="replace")
        for m in _VAR_TH.finditer(txt):
            # fallback = 逗号后到配平右括号（可能含 rgba(...) 嵌套）
            k = m.end()
            depth = 1  # 已在 var( 内
            start = k
            while k < len(txt) and depth > 0:
                if txt[k] == "(":
                    depth += 1
                elif txt[k] == ")":
                    depth -= 1
                k += 1
            yield rel, m.group(1), txt[start:k - 1].strip()


def test_theme_tokens_css_mounted_in_roots():
    """token CSS 漏挂＝该系页面暗色不翻转（fallback 兜底亮色仍对——软失败变硬）。"""
    missing = []
    for root in _TOKEN_ROOTS:
        txt = (TEMPLATES_ROOT / root).read_text(encoding="utf-8", errors="replace")
        if "/static/theme-tokens.css" not in txt:
            missing.append(root)
    assert not missing, (
        "根布局缺 theme-tokens.css 挂载（<link rel=\"stylesheet\" "
        "href=\"/static/theme-tokens.css?v=...\">）：\n  " + "\n  ".join(missing)
    )


def test_th_token_fallbacks_match_css_light_values():
    """模板 fallback ≡ :root 亮值：迁移的"亮色零变化"承诺由此钉死。

    若要调整某 token 的亮色值：改 tools/inline_color_codemod.py 的表 →
    --emit-css 重生成 → 模板里旧 fallback 会被本门禁点名，跑 codemod 或手改对齐。
    """
    css = THEME_CSS.read_text(encoding="utf-8")
    light = _parse_css_block(css, ":root")
    bad = []
    for rel, token, fb in _iter_template_var_refs():
        defined = light.get(token)
        if defined is None:
            continue  # 未定义由下一个测试点名
        if _canon_color(fb) != _canon_color(defined):
            bad.append(f"{rel}: {token} fallback={fb!r} ≠ :root {defined!r}")
    assert not bad, "模板 var(--th-*) fallback 与 theme-tokens.css :root 亮值漂移：\n  " + "\n  ".join(bad[:40])


def test_th_tokens_all_defined():
    """模板引用的 --th-* 必须在 theme-tokens.css 有定义（防拼错静默回落）；
    暗色块只允许覆写 :root 已有的 token（防"只有暗值没有亮值"的幽灵 token）。"""
    css = THEME_CSS.read_text(encoding="utf-8")
    light = _parse_css_block(css, ":root")
    # 暗色块必须同时挂两套开关（base 系 data-theme / 工作台 data-cp-theme）——
    # 少一个=对应页面家族暗色不翻转（P3-2 教训：曾只认 data-theme，工作台白开）。
    dark = _parse_css_block(css, '[data-theme="dark"],[data-cp-theme="dark"]')
    undefined = sorted({
        f"{token}（{rel}）" for rel, token, _fb in _iter_template_var_refs()
        if token not in light
    })
    assert not undefined, "模板引用了未定义的主题 token：\n  " + "\n  ".join(undefined[:20])
    orphan_dark = sorted(set(dark) - set(light))
    assert not orphan_dark, "暗色块存在 :root 没有的 token：\n  " + "\n  ".join(orphan_dark)
