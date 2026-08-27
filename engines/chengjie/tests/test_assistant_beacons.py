# -*- coding: utf-8 -*-
"""小智面板「意图埋点 + 画布令牌 + 入口行观感」门禁（实施73 P0，2026-08-27）。

## 为什么需要这道门禁

2026-08-01..27 的线上 `ui_event_trend` 读数暴露了一个方法论级缺陷：球只发
``asb_orb_*``（boot / listen / degrade / say / calm）——**全是球自身的生命周期
事件**，boot 更是「页面加载即发」而非用户意图。于是：

- 「有多少人真的点开了小智」没有任何数据；
- 「点开后去了哪个页签 / 问了没问 / 点没点大家常问」全部空白；
- 唯一的功能类事件（教学 2 / 代办 4 / 配对 1）**集中在 08-23 单日**，
  那是实施62 的开发自测日；此后 7 次 boot 零功能事件。

没有分母时，那 7 次曾被当成「真实用量偏低」来解读——实际连基线都还没建立。
本门禁把补上的分母**钉成不可静默回退的契约**：埋点是漏斗的地基，删一枚就
少一段真相，而删掉它不会有任何运行时报错（sendBeacon 是 best-effort 设计）。

## 三组不变量

1. **意图埋点在场**（漏斗分母 + P1 删除决策的裁决依据）
2. **画布令牌单源** ``--xz-*``（三模块合一；P1 模式条的前提）
3. **入口行观感**：不得回退成 dashed 边框 / 截断 hint（P0-3 两处实锤）
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "shared" / "assistant"
BALL = SHARED / "assistant-ball.js"
TEACH = SHARED / "assistant-teach.js"
AGENT = SHARED / "assistant-agent.js"
ALL_THREE = (BALL, TEACH, AGENT)

# 旧的三套私有前缀——合并后任何一个重新出现都意味着有人把令牌拆回去了。
# 只匹配**真实用法形态**（CSS 的 var(--x- / JS 对象键与 setProperty 的 '--x-）
# 而不是裸子串：文件头注释要讲清「原先是哪三套前缀」这段历史，裸子串会把
# 文档本身判成违规（首次实施即踩，2026-08-27）。
LEGACY_TOKEN_USES = tuple(
    f"{lead}{pfx}"
    for pfx in ("--as-", "--xzt-", "--xza-")
    for lead in ("var(", "'", '"')
)

# 画布令牌的规范键集（三文件的 themeVars 必须逐字等值）
CANONICAL_TOKENS = (
    "--xz-bg", "--xz-bd", "--xz-txt", "--xz-muted", "--xz-accent", "--xz-input",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── 1. 意图埋点 ──────────────────────────────────────────────────────────────

def test_intent_beacons_present():
    """漏斗分母不得被静默删除。

    每一枚都对应一个**具体的、当前答不上来的问题**：
      asb_open        有多少人真的点开了球（对照 asb_orb_boot 得打开率）
      asb_open_ext    程序化开面板（教学/代办投问）——必须与人点开分开计
      asb_tab_*       点开后去了哪个页签
      asb_ask         问答主链有没有被用
      asb_chip        「大家常问」有没有人点（P1 要搬走它，得先有证据）
      asb_h3_*        欢迎三卡的真实用量（P1 计划删除，删前需裁决依据）
      asb_report_*    报障提交量 vs 成功量（差值＝静默提交失败）
    """
    src = _read(BALL)
    required = {
        "'asb_open'": "点开球（打开率的分子）",
        "'asb_open_ext'": "程序化开面板（不得混进打开率）",
        "'asb_tab_'": "页签切换（去向分布）",
        "'asb_ask'": "问答主链计数",
        "'asb_chip'": "大家常问点击（P1 搬迁裁决）",
        "'asb_h3_ask'": "欢迎卡·问功能（P1 删除裁决）",
        "'asb_h3_report'": "欢迎卡·报障（P1 删除裁决）",
        "'asb_h3_teach'": "欢迎卡·学操作（P1 删除裁决）",
        "'asb_report_submit'": "报障提交",
        "'asb_report_ok'": "报障成功（与 submit 的差＝失败率）",
    }
    for token, why in required.items():
        assert token in src, (
            f"assistant-ball.js 缺埋点 {token}（{why}）——"
            "sendBeacon 是 best-effort，删掉它不会报错，只会让漏斗少一段真相"
        )


def test_beacon_helper_shares_orb_channel():
    """意图埋点必须复用 orbBeacon 的通道与守卫（协议/非 http 跳过/sendBeacon
    存在性检查），不许另起一条 fetch——第二条通道＝第二套失败模式。"""
    src = _read(BALL)
    assert re.search(r"function beacon\(action\)\s*\{\s*orbBeacon\(action\);\s*\}", src), (
        "beacon() 不再是 orbBeacon 的薄封装——两条埋点通道会各自漂移"
    )
    # 全文件只允许一处 sendBeacon 实体调用（orbBeacon 内）
    assert src.count("navigator.sendBeacon(") == 1, (
        "出现第二处 navigator.sendBeacon——埋点通道必须单点"
    )


def test_programmatic_paths_do_not_pollute_denominator():
    """打开率与页签分布是**人的意图**读数，程序化路径必须显式静默。

    两个具体污染源（都踩过才知道）：
      - renderPanel 每次都 switchTab('chat')：不静默则 asb_tab_chat 恒等于
        开面板数，「坐席主动切到对话」的信号被稀释成噪声；
      - askFromOutside（教学/代办投问）会 togglePanel(true)：不标 'ext'
        则打开率虚高，而打开率正是本批要建立的基线。
    """
    src = _read(BALL)
    assert "switchTab('chat', true)" in src, (
        "renderPanel 的初始化切页签未静默——asb_tab_chat 将等于开面板数"
    )
    assert "togglePanel(true, 'ext')" in src, (
        "askFromOutside 未标记 ext——程序化开面板会混进人工打开率"
    )
    assert "beacon(src === 'ext' ? 'asb_open_ext' : 'asb_open')" in src, (
        "togglePanel 的开面板归因分流丢失"
    )


def test_open_beacon_fires_only_on_transition():
    """togglePanel(true) 在面板已开时会被重复调用（外部投问、深链）。
    必须只在 关→开 跃迁时计数，否则打开率被重复触发灌水。"""
    src = _read(BALL)
    assert "var wasOpen = S.open;" in src and "if (S.open && !wasOpen)" in src, (
        "togglePanel 缺 关→开 跃迁守卫——重复调用会把打开数灌高"
    )


# ── 2. 画布令牌单源 ──────────────────────────────────────────────────────────

def test_no_legacy_token_prefixes():
    """三套私有前缀（--as-/--xzt-/--xza-）已合一为 --xz-*。

    合并的理由不是洁癖：三份 themeVars 原本逐字节相同（同一批宿主 token 抄了
    三遍），任一处漂移就会让三个模块**在同一个面板里长得不一样**；而 P1 的
    模式条要求三者共用一套度量，不合并就做不出来。
    """
    for p in ALL_THREE:
        src = _read(p)
        for bad in LEGACY_TOKEN_USES:
            assert bad not in src, (
                f"{p.name} 出现旧令牌用法 {bad!r}——画布令牌必须保持 --xz-* 单源"
            )


def test_canonical_theme_tokens_identical_across_modules():
    """三文件的 themeVars 必须覆盖同一套规范键（等值副本）。

    三个独立 IIFE 无模块系统 → 复制是被迫的；正因为是复制，才必须有门禁盯住
    它们不漂移（这正是合并前的老毛病）。
    """
    for p in ALL_THREE:
        src = _read(p)
        for key in CANONICAL_TOKENS:
            assert f"'{key}'" in src, (
                f"{p.name} 的 themeVars 缺规范令牌 {key}——三模块令牌集已漂移"
            )
        # 两个宿主壳的映射都要在（workspace 用 --tk-*，admin 用 --card/--bd/…）
        assert "var(--tk-surface,#fff)" in src, f"{p.name} 缺 workspace 壳映射"
        assert "var(--card,#fff)" in src, f"{p.name} 缺 admin 壳映射"


# ── 3. 入口行观感（P0-3 两处实锤的回归钉）────────────────────────────────────

def test_entry_rows_are_not_dashed():
    """虚线边框在设计系统里是「占位 / 未完成 / 拖放区」语义。

    小智把**主打功能**（教学模式 / 替我做 / 手机操控）做成虚线胶囊，用户的
    第一反应是「这个还没做好」，不敢点。改回 dashed 就是把这个观感请回来。

    注意：`.xzt-hover`（教学模式的悬停高亮框）用 dashed 是**正确**语义
    ——那是选取指示器不是按钮，故本门禁只约束 `-row button`。
    """
    for p, cls in ((TEACH, "xzt-row"), (AGENT, "xza-row")):
        src = _read(p)
        m = re.search(rf"\.{cls} button\{{([^}}]*)\}}", src)
        assert m, f"{p.name} 找不到 .{cls} button 规则（选择器被改名？）"
        rule = m.group(1)
        assert "dashed" not in rule, (
            f"{p.name} 的 .{cls} button 又变回 dashed 边框——"
            "主打功能不该长成「未完成占位」的样子"
        )
        assert "solid" in rule, f"{p.name} 的 .{cls} button 缺实线边框"


def test_entry_row_hint_is_not_truncated():
    """入口行 hint 承载的是**打消顾虑的安全承诺**，不能被省略号吃掉。

    实锤：教学行 hint 全文是「进入后点击页面任意位置看讲解，不会真的执行」，
    在 nowrap+ellipsis 下显示为「…不会真…」——最该看全的半句被截断，
    这正是「没人敢开教学模式」的直接诱因之一。代办行同理（「改设置前必先确认」
    是「替我做会不会乱改我配置」的答案）。

    修法＝hint 独占整行（flex:1 0 100%）自然换行；行高多一行是刻意代价。
    """
    for p, cls in ((TEACH, "xzt-row"), (AGENT, "xza-row")):
        src = _read(p)
        m = re.search(rf"\.{cls} \.hint\{{([^}}]*)\}}", src)
        assert m, f"{p.name} 找不到 .{cls} .hint 规则"
        rule = m.group(1)
        assert "text-overflow:ellipsis" not in rule and "nowrap" not in rule, (
            f"{p.name} 的 .{cls} .hint 又变回截断——安全承诺会被吃掉半句"
        )
        assert "100%" in rule, (
            f"{p.name} 的 .{cls} .hint 未独占整行，换行不生效"
        )
        assert f".{cls}{{" in src and "flex-wrap:wrap" in src, (
            f"{p.name} 的 .{cls} 缺 flex-wrap:wrap，hint 无法换到第二行"
        )


def test_safety_promise_text_is_complete():
    """两行 hint 的中英文案都必须包含那句承诺的**完整语义**。
    文案被改短（为了塞进一行）等于绕过上面的布局修复。"""
    teach = _read(TEACH)
    assert "不会真的执行" in teach, "教学 hint 丢了「不会真的执行」的承诺"
    assert "nothing is executed" in teach.lower(), "教学 hint 英文承诺缺失"
    agent = _read(AGENT)
    assert "改设置前必先确认" in agent, "代办 hint 丢了「必先确认」的承诺"
    assert "confirm" in agent.lower(), "代办 hint 英文承诺缺失"
