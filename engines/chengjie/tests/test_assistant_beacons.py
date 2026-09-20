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

1. **意图埋点在场**（漏斗分母；P1 起分模式/页签两类计）
2. **画布令牌单源** ``--xz-*``（三模块合一；P1 模式条的前提）
3. **模式化信息架构**（实施73 P1，2026-08-28）：三大功能经 ``registerMode``
   认领模式条一格而非往面板插行；次级页签顺序＝老板拍板的 报障·我的·常问·⚙；
   手机操控在标题栏而非第四个模式格；安全承诺整段常驻不被截断；主按钮不是
   虚线占位；欢迎三卡不得复活。
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
      asb_chip        本页常问 chip 有没有人点
      asb_mode_*      三大功能的模式分布（P1 的唯一硬指标：入口触达率）
      asb_faq_*       常问面板搜了没搜、点没点（P1-6 的验收判据）
      asb_report_*    报障提交量 vs 成功量（差值＝静默提交失败）

    P1（2026-08-28）：``asb_h3_*`` 随欢迎三卡一并退场——那三枚埋点的使命就是
    给「该不该删这三张重复卡」提供裁决依据，卡删了它们也就没有观测对象了。
    """
    src = _read(BALL)
    required = {
        "'asb_open'": "点开球（打开率的分子）",
        "'asb_open_ext'": "程序化开面板（不得混进打开率）",
        "'asb_tab_'": "次级页签切换（报障/我的/常问/⚙ 去向）",
        "'asb_mode_'": "模式切换（问答/教学/替我做 三档分布）",
        "'asb_ask'": "问答主链计数",
        "'asb_chip'": "本页常问 chip 点击",
        "'asb_faq_search'": "常问面板搜索（P1-6 有没有人用）",
        "'asb_faq_pick'": "常问条目点击→直接发问（该面板的价值兑现点）",
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


# ── 3. 模式化信息架构（实施73 P1，2026-08-28）───────────────────────────────

def test_three_features_claim_modes_not_panel_rows():
    """三大功能必须是**模式条里的一格**，不能退回「往面板底部插一行」。

    旧模型（`.xzt-row` / `.xza-row` + MutationObserver 自愈）有两个致命面：
    入口挤在面板最下缘最不显眼处；且那一行挂在面板层，**报障/我的页签下
    也跟着显示**——每个页签都不清爽正是老板报障的那张截图。

    退回插行不会有任何运行时报错（那正是它当初能长期存在的原因），故靠
    静态门禁点名。
    """
    for p, cls in ((TEACH, "xzt-row"), (AGENT, "xza-row")):
        src = _read(p)
        assert f"'.{cls}'" not in src and f'"{cls}"' not in src, (
            f"{p.name} 又出现 .{cls} 插行——三大功能应经 registerMode 认领模式"
        )
        assert "registerMode" in src, (
            f"{p.name} 未经 AssistantBall.registerMode 认领模式——"
            "模式条里会缺这一格，功能变成不可达"
        )
        assert "mount:" in src, f"{p.name} 模式定义缺 mount（没有内容可挂）"
    ball = _read(BALL)
    assert "registerMode: registerMode" in ball, (
        "AssistantBall 不再暴露 registerMode——姊妹组件无从认领模式"
    )
    # 缺席的姊妹组件不得留下点不动的空格子：单模式时整条隐藏
    assert "classList.toggle('solo'" in ball, (
        "模式条缺单模式隐藏（solo）——组件缺席会留一个点不动的段控"
    )


def test_mode_bar_is_accessible_segmented_control():
    """段控不是三个按钮摆一排：radiogroup 语义 + 方向键 + 减动效尊重。

    这三条都属于「不做也能用、但做了才不像半成品」的那类——而且事后补
    比一开始写贵得多（要重排 DOM）。
    """
    ball = _read(BALL)
    assert 'role="radiogroup"' in ball, "模式条缺 radiogroup 语义"
    assert 'role="radio"' in ball and "aria-checked" in ball, (
        "模式格缺 radio/aria-checked——读屏用户听不出当前在哪个模式"
    )
    assert "ArrowRight" in ball and "ArrowLeft" in ball, (
        "模式条缺方向键切换（radiogroup 的标准交互）"
    )
    m = re.search(r"\.asb-modes-ind\{([^}]*)\}", ball)
    assert m and "transition:" in m.group(1), "滑块指示器缺过渡动画"
    assert re.search(
        r"prefers-reduced-motion:reduce\)\{\.asb-modes-ind\{transition:none\}",
        ball), "减动效偏好下未禁用滑块动画"


def test_secondary_tabs_order_report_mine_faq_settings():
    """次级页签顺序是**老板拍板的**：报障 · 我的 · 常问 · ⚙。

    「常问排在我的之后」是明确要求（原本它散在对话首屏挤版面）。顺序被
    重排不会报错，只会让人找不到——故钉死相对位置。
    """
    ball = _read(BALL)
    block = ball.split("function renderSubtabs", 1)[-1].split("\n  }", 1)[0]
    order = re.findall(r'data-tab="([a-z]+)"', block)
    assert order == ["report", "mine", "faq", "set"], (
        f"次级页签顺序漂移：{order}；老板拍板的顺序是 报障·我的·常问·⚙"
    )
    assert "asb_faq_search" in ball and "asb_faq_pick" in ball, (
        "常问面板缺埋点——P1 验收判据「faq 有非零流量」将无从判断"
    )


def test_phone_control_lives_in_title_bar_not_mode_bar():
    """手机操控是「替我做」的远程输入端，**不是第四种能力**（§3.1 已拍板）。

    做成第四个平级模式会让用户以为那是另一套功能，反而更看不出它与替我做
    的关系——这正是配对成功数长期为 0 的诱因之一。落法＝标题栏图标 +
    连接状态点，另在替我做模式内给一张说明卡（两处曝光，语义诚实）。
    """
    ball = _read(BALL)
    assert 'class="asb-hd-pair"' in ball, "标题栏缺手机操控入口"
    assert "/api/assistant/pair/sessions" in ball, (
        "状态点未读真实配对态——绿灯必须对应真的连着手机"
    )
    assert "registerMode('agent'" in _read(AGENT), "替我做未认领模式"
    assert "registerMode('pair'" not in _read(AGENT), (
        "手机操控被做成了第四个模式格——违反 §3.1 已拍板的信息架构"
    )
    assert "pair_card_t" in _read(AGENT), "替我做模式内缺「用手机指挥」说明卡"


def test_safety_promise_is_full_line_not_truncated():
    """安全承诺承载「会不会真的点下去 / 会不会乱改我配置」的答案。

    实锤（P0-3）：它曾被塞进入口行 nowrap+ellipsis 里显示成「…不会真…」，
    最该看全的半句被吃掉。P1 把它升为模式卡内常驻整段（`.asb-md-safe`），
    本门禁钉住它不得再被截断、且两个模式都真的渲染它。
    """
    ball = _read(BALL)
    m = re.search(r"\.asb-md-safe\{([^}]*)\}", ball)
    assert m, "球缺 .asb-md-safe 规则（模式卡的安全承诺样式）"
    rule = m.group(1)
    assert "text-overflow:ellipsis" not in rule and "nowrap" not in rule, (
        ".asb-md-safe 又变回截断——安全承诺会被吃掉半句"
    )
    for p in (TEACH, AGENT):
        src = _read(p)
        assert "asb-md-safe" in src and "t('row_hint')" in src, (
            f"{p.name} 的模式卡没渲染安全承诺"
        )


def test_primary_mode_action_is_not_dashed():
    """虚线边框在设计系统里是「占位 / 未完成 / 拖放区」语义。

    主打功能长成虚线胶囊，用户第一反应是「这个还没做好」，不敢点。
    模式卡的主按钮必须是实心主色，副按钮实线描边。
    （`.xzt-hover` 悬停高亮框用 dashed 是**正确**语义——那是选取指示器
    不是按钮，故本门禁只约束模式卡按钮。）
    """
    ball = _read(BALL)
    go = re.search(r"\.asb-md-go\{([^}]*)\}", ball)
    assert go, "球缺 .asb-md-go 规则（模式主按钮）"
    assert "dashed" not in go.group(1), ".asb-md-go 变回 dashed 边框"
    assert "background:var(--xz-accent" in go.group(1), (
        ".asb-md-go 不再是实心主色按钮——主行动召唤退化成了普通链接观感"
    )
    sub = re.search(r"\.asb-md-b\{([^}]*)\}", ball)
    assert sub, "球缺 .asb-md-b 规则（模式副按钮）"
    assert "dashed" not in sub.group(1) and "solid" in sub.group(1), (
        ".asb-md-b 缺实线描边"
    )


def test_welcome_h3_cards_stay_deleted():
    """欢迎三卡（问功能 / 报个障 / 学操作）是同一批入口的第三次重复。

    问功能=输入框、报障=次级页签、学操作=模式条第二格——三张卡只是在挤
    首屏（首屏可点目标从 ~17 降到 ~12 主要就是删了它们）。这是反向门禁：
    防「顺手加回一排快捷卡」的复发。
    """
    ball = _read(BALL)
    assert "asb-h3" not in ball, "欢迎三卡回归了——首屏重复入口又挤回来"
    assert "h3_ask" not in ball and "h3_teach" not in ball, (
        "三卡词条残留——删卡要连词条一起回收，否则下次有人照着又渲染一遍"
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
