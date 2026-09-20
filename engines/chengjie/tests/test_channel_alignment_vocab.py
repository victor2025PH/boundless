# -*- coding: utf-8 -*-
"""渠道中心四页「五段式词表 / KPI 共享词 / 控制条」对齐契约（P1，2026-08-02 定稿）。

方针（运营拍板）：
- 子 Tab 统一词表 = `chc_tab_*`（监控·待审·配置·语音·运维·漏斗），四页共用单一词源；
  平台**有该段才显示**（子集原则，不硬凑空 Tab），平台特有 Tab（模板分析/线索/人设/设备）
  保留各自词条**缀尾**，不得插进公共段中间。
- Hero KPI 共享词 = `chc_kpi_sent`（AI 已发）/ `chc_kpi_pending`（待处理）——只统一
  「≥2 页共有的概念」。Telegram：`chc_kpi_sent` 已接入（P2-0，daily_stats.replies，
  sender._postsend_record_count 埋点；结构化口径缺席时前端如实显 —）；`chc_kpi_pending`
  仍豁免——TG A 线无待审概念（自动链直发，人审走坐席收件箱），勿硬凑。
- 控制条统一 = `chc_act_*`：触发 → 暂停时长(select) → 暂停 → 恢复 → 手动发送；
  暂停时长档位三页同值（300/900/1800/3600/10800s）。WhatsApp 无控制条手动发送入口
  （发送在会话详情/发送队列），刻意豁免该钮。

核心不变量：预判词表与模板实际用键**完全一致**——词表另算一套比没有词表更糟。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL_DIR = _ROOT / "src" / "web" / "templates"
_BODIES = {
    "telegram": _TPL_DIR / "_channel_body_telegram.html",
    "line": _TPL_DIR / "_channel_body_line.html",
    "whatsapp": _TPL_DIR / "_channel_body_whatsapp.html",
    "messenger": _TPL_DIR / "_channel_body_messenger.html",
}

# 每页主 Tab 栏应出现的键，按显示顺序（公共段子集按 监控→待审→配置→语音→运维→漏斗 排序，
# 平台特有 Tab 缀尾）。
_TAB_ORDER = {
    "telegram": ["chc_tab_config", "chc_tab_voice", "chc_tab_ops", "chc_tab_funnel"],
    "line": ["chc_tab_monitor", "chc_tab_pending", "chc_tab_config",
             "chc_tab_ops", "chc_tab_funnel"],
    "whatsapp": ["chc_tab_monitor", "chc_tab_pending", "chc_tab_config",
                 "chc_tab_ops", "chc_tab_funnel", "wa_s020"],
    "messenger": ["chc_tab_monitor", "chc_tab_pending", "chc_tab_config",
                  "chc_tab_ops", "chc_tab_funnel", "msg_s021", "msg_s022",
                  "msg_s023"],
}


def _read(plat: str) -> str:
    return _BODIES[plat].read_text(encoding="utf-8")


def _main_tabbar(plat: str, html: str) -> str:
    """取主 Tab 栏块：TG/LINE/WA = 首个 .st-bar；Messenger = .mr-tabs。"""
    marker = 'class="mr-tabs"' if plat == "messenger" else 'class="st-bar"'
    start = html.index(marker)
    end = html.index("</div>", start)
    return html[start:end]


def test_main_tab_bars_use_shared_vocab_in_canonical_order():
    for plat, expected in _TAB_ORDER.items():
        bar = _main_tabbar(plat, _read(plat))
        positions = []
        for key in expected:
            pos = bar.find(f"'{key}'")
            assert pos >= 0, f"{plat}: 主 Tab 栏缺统一词表键 {key}"
            positions.append(pos)
        assert positions == sorted(positions), (
            f"{plat}: Tab 顺序偏离五段式定稿 {expected}（公共段固定序，平台特有缀尾）")


def test_no_stale_tab_keys_left_in_tab_bars():
    """旧 Tab 键不得残留在主 Tab 栏（词表切换必须彻底，防"一半新词一半旧词"）。"""
    stale = {
        "telegram": ("tg_s013", "wa_s098", "ov_js_th_account", "rpa_fn_title"),
        "line": ("ln_s014", "ln_s015", "wa_s021", "rpa_fn_title"),
        "whatsapp": ("wa_s019", "ov_kpi_pending", "msg_js_1056", "wa_s021"),
        "messenger": ("msg_s020", "msg_s024", "msg_s025", "msg_s026"),
    }
    for plat, keys in stale.items():
        bar = _main_tabbar(plat, _read(plat))
        for key in keys:
            assert f"'{key}'" not in bar, f"{plat}: 主 Tab 栏残留旧词条键 {key}"


def _kpi_row(html: str) -> str:
    start = html.index("rpa-kpi-row")
    end = html.index("rpa-hero-actions", start) if "rpa-hero-actions" in html[start:] \
        else start + 4000
    return html[start:end]


def test_hero_kpi_shared_vocab_on_line_wa_msg():
    """四页 hero 首排必须带「AI 已发」共享词；「待处理」LINE/WA/MSG 必带、TG 豁免
    （无待审概念，见模块 docstring）。"""
    for plat in ("telegram", "line", "whatsapp", "messenger"):
        row = _kpi_row(_read(plat))
        assert "'chc_kpi_sent'" in row, f"{plat}: hero 缺共享 KPI 键 chc_kpi_sent"
        if plat != "telegram":
            assert "'chc_kpi_pending'" in row, f"{plat}: hero 缺共享 KPI 键 chc_kpi_pending"


def test_hero_pending_kpi_has_fill_site():
    """共享 KPI 不是摆设：每页 JS 必须有对应填充点（与队列/状态/当日计数同源）。

    P2-3 起 Messenger 主 JS 外迁 static/messenger/messenger_rpa.js —— 计数口径
    = 模板 + 模板引用的本地静态 JS（与哑按钮门禁同一解析器）。"""
    from tests import _inline_handler_scan as scan
    static_root = _ROOT / "src" / "web" / "static"
    fills = {
        "line": "lr-kpi-pending",
        "whatsapp": "wa-kpi-pending",
        "messenger": "kpi-pending",
        "telegram": "st-replies",
    }
    for plat, el_id in fills.items():
        html = _read(plat)
        blob = html + "".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in scan.local_static_scripts(html, static_root))
        # 标记里出现 1 次 + JS 里至少再出现 1 次（getElementById/$()）
        assert blob.count(el_id) >= 2, f"{plat}: {el_id} 无 JS 填充点（KPI 会恒 0/—）"


def test_control_bar_shared_vocab_and_pause_tiers():
    """控制条三页（LINE/WA/MSG）同词同序同档位；TG 无轮询触发概念，整条豁免。"""
    selects = {"line": "lr-pause-select", "whatsapp": "wa-pause-select",
               "messenger": "mr-pause-select"}
    tiers = ('value="300"', 'value="900"', 'value="1800"',
             'value="3600"', 'value="10800"')
    for plat, sel_id in selects.items():
        html = _read(plat)
        i_trigger = html.find("'chc_act_trigger'")
        i_select = html.find(sel_id)
        i_pause = html.find("'chc_act_pause'")
        i_resume = html.find("'chc_act_resume'")
        assert min(i_trigger, i_select, i_pause, i_resume) >= 0, (
            f"{plat}: 控制条缺统一词表键/时长选择（trigger={i_trigger} select={i_select} "
            f"pause={i_pause} resume={i_resume}）")
        assert i_trigger < i_select < i_pause < i_resume, (
            f"{plat}: 控制条顺序偏离定稿 触发→时长→暂停→恢复")
        sel_block = html[i_select:i_select + 900]
        for t in tiers:
            assert t in sel_block, f"{plat}: 暂停时长档位缺 {t}（三页须同档）"
    # 手动发送：LINE/MSG 有入口；WA 刻意豁免（发送在会话详情）
    for plat in ("line", "messenger"):
        assert "'chc_act_send_manual'" in _read(plat), f"{plat}: 缺手动发送统一词条"


def test_shared_vocab_keys_exist_bilingual():
    """模板引用的 chc_* 共享词必须在 channel_center pack 里 zh/en 双语齐备。"""
    import importlib
    import sys
    sys.path.insert(0, str(_ROOT))
    pack = importlib.import_module("src.web.i18n_packs.channel_center")
    needed = [
        "chc_tab_monitor", "chc_tab_pending", "chc_tab_config",
        "chc_tab_voice", "chc_tab_ops", "chc_tab_funnel",
        "chc_kpi_sent", "chc_kpi_pending",
        "chc_act_trigger", "chc_act_pause", "chc_act_resume",
        "chc_act_send_manual", "chc_act_pause_5m", "chc_act_pause_15m",
        "chc_act_pause_30m", "chc_act_pause_1h", "chc_act_pause_3h",
    ]
    for key in needed:
        assert pack.ZH.get(key), f"channel_center.ZH 缺 {key}"
        assert pack.EN.get(key), f"channel_center.EN 缺 {key}"


def test_messenger_card_family_migrated():
    """P2-1 收口棘轮（2026-08-02）：Messenger 正文 27 个 mr-panel 已全量迁到共享
    rpa-card 族，mr-panel 卡壳 CSS 已删——wrapper/head/title/sub 不得回潮（新面板
    照旧模板抄回来会在无样式定义下渲染成裸块），rpa-card 族保有量设下限防回退。
    mr-advanced-only（密度档修饰类）与 mr-grid/mr-stack 等布局工具类不在此列。"""
    html = _read("messenger")
    for cls in ('class="mr-panel"', 'class="mr-panel ',
                "mr-panel-head", "mr-panel-title", "mr-panel-sub"):
        assert cls not in html, f"messenger: mr-panel 卡壳类回潮 {cls}（族已删除，请用 rpa-card）"
    assert html.count('class="rpa-card') >= 80, "messenger: rpa-card 族保有量跌破下限"


def test_messenger_main_js_relocated():
    """P2-3 收口（2026-08-02）：主 IIFE 已外迁 static/messenger/messenger_rpa.js——
    模板不得再长出 >60KB 的单块内联 JS（防止有人把大段业务 JS 抄回模板，
    热区/缓存/键门禁三重收益全部回吐）；外迁文件必须仍被模板以 ?v= 戳引用。"""
    import re
    html = _read("messenger")
    assert re.search(
        r'<script src="/static/messenger/messenger_rpa\.js\?v=\w+"></script>', html), \
        "messenger: 外迁主脚本的 <script src>（含 ?v= 戳）引用丢失"
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I):
        if re.search(r"\bsrc\s*=", m.group(1) or "", re.I):
            continue
        assert len(m.group(2)) < 60000, (
            "messenger: 模板内又出现超大内联 JS 块（应放 static/messenger/ 并加 ?v= 戳）")
    js = (_ROOT / "src" / "web" / "static" / "messenger" / "messenger_rpa.js")
    assert js.is_file() and js.stat().st_size > 100000, "外迁主脚本文件缺失或异常缩水"


def test_line_pending_pane_promoted():
    """LINE 待审已从运维 pane 提升为独立 Tab：审核队列卡必须在 pane-pending 内，
    且运维 pane 不再包含它（防止回迁或双份）。"""
    html = _read("line")
    assert 'id="pane-pending"' in html
    i_pane = html.index('id="pane-pending"')
    i_card = html.index('id="lr-pending-card"')
    i_ops = html.index('id="pane-ops"')
    i_funnel = html.index('id="pane-funnel"')
    assert i_ops < i_pane, "pane 顺序异常"
    assert i_pane < i_card < i_funnel, "审核队列卡不在 pane-pending 块内"
    assert html.count('id="lr-pending-card"') == 1
