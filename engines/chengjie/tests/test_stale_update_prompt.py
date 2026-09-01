# -*- coding: utf-8 -*-
"""「界面有新版本」提示链的静态契约门禁（2026-08-28 老板实录事故沉淀）。

事故原文：右下胶囊显示「工作台界面已更新到新版本，刷新页面即可加载（…」——
**桌面壳是 Electron webview，没有地址栏刷新按钮**，坐席被要求执行一个产品里不
存在的动作；更要命的是唯一带「立即刷新」按钮的卡片 ``ttlMs:20000`` 20 秒后自动
消失，之后胶囊无按钮、消息中心「进行中」条目 ``cursor:default`` 也无按钮——
**持续状态却只有瞬时动作**，坐席离开工位一会儿回来就彻底没有出路。

本门禁钉住修复后的不变量（每条都对应一个真实失效面，别当形式主义删）：

1) 总线：``ongoing.set`` 收 ``action`` 且有 ``invoke``——常驻状态必须常驻可执行；
2) 胶囊：文本 2 行 clamp（不再 ``nowrap`` 硬截断成半句）、结构 button 化（键盘可
   达）、全文进 title/aria-label（截断了也要能读到整句）；
3) 三处宿主（工作台 / 经典后台 / 副驾 App）文案不得再出现「刷新页面/刷新此面板」
   这类**浏览器操作术语**，只描述结果；
4) 工作台：未发送媒体保护（暂存图片/语音会被重载清空 → 必须先问）+ 空闲静默换版
   （最好的更新提示是不需要用户动手）；
5) 经典后台壳**刻意不自动重载**（后台多是编辑中的长表单，代价 > 收益）——这是
   有意的不对称，反向钉住防「顺手统一」；
6) severity 分级贯通：发布侧 ``bump_ui_build.py --severity`` 写、三处探针读。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
BUS = REPO / "src" / "web" / "static" / "workspace" / "notify-bus.js"
WSB = REPO / "src" / "web" / "templates" / "workspace_base.html"
BASE = REPO / "src" / "web" / "templates" / "base.html"
PACK_WS = REPO / "src" / "web" / "i18n_packs" / "inbox_workspace.py"
PACK_HB = REPO / "src" / "web" / "i18n_packs" / "help_ball.py"
BUMP = REPO / "scripts" / "bump_ui_build.py"
STAMP = REPO / "src" / "web" / "static" / "workspace" / "ui-build.txt"

_COPILOT_TREES = (
    REPO / "shared" / "copilot",
    REPO / "desktop" / "renderer" / "shared" / "copilot",
)


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _both(rel: str):
    """副驾双树同一份文件（网页源 + 桌面镜像），任一漏改都是「一半用户还在踩」。"""
    return [(t / rel, _read(t / rel)) for t in _COPILOT_TREES]


# ── 1) 总线：常驻状态 → 常驻动作 ────────────────────────────────────────────
def test_ongoing_capsule_supports_persistent_action():
    src = _read(BUS)
    assert "action:" in src and "invoke: function(id)" in src, (
        "ongoing 必须支持 action + invoke——胶囊没有按钮就是本次事故的根因")
    assert "actionLabel" in src, "snapshot 要带 actionLabel，消息中心才镜像得出按钮"
    assert "wsntf-act" in src, "缺胶囊动作按钮的样式/类名"


def test_capsule_text_not_single_line_truncated():
    """截图实锤：320px + 单行不换行把整句提示截成了半句（省略号后什么都看不到）。"""
    css = _read(BUS)
    css = css[css.find("#ws-status-capsule"):css.find("document.head.appendChild")]
    css = css.replace(" ", "").replace("'\n+'", "")   # 拼接字符串拼回连续 CSS
    txt_rule = re.search(r"\.wsntf-txt\{([^}]*)\}", css)
    assert txt_rule, "找不到胶囊正文样式规则"
    assert "white-space:nowrap" not in txt_rule.group(1), (
        "胶囊正文不得单行硬截断；用 -webkit-line-clamp 2 行封顶（按钮那条 nowrap 是对的）")
    assert "-webkit-line-clamp:2" in txt_rule.group(1)


def test_capsule_is_keyboard_reachable_and_reads_full_text():
    src = _read(BUS)
    assert "main.type = 'button'" in src, "胶囊主体必须是 button（div+click 键盘不可达）"
    assert "focus-visible" in src, "缺焦点环"
    assert "main.setAttribute('aria-label', full)" in src and "main.title = full" in src, (
        "全文必须进 title + aria-label——文本被 clamp 时这是唯一读得到整句的地方")


def test_capsule_colors_follow_theme_tokens():
    """亮色工作台右下角挂一块恒定深色药丸是异物；fallback 保住无 --tk-* 的经典后台壳。"""
    css = _read(BUS)
    for tok in ("var(--tk-surface,", "var(--tk-text,", "var(--tk-border,"):
        assert tok in css, f"胶囊配色未走主题令牌：{tok}"


# ── 2) 三处宿主：文案去浏览器术语 ───────────────────────────────────────────
# 浏览器操作术语（桌面壳 Electron webview 无地址栏/刷新按钮，说了也做不到）。
# 拆成片段拼接：本文件自己的说明文字里若原样出现该词，会把门禁打红（AGENTS
# 「注释里不能出现你正要禁掉的那个字面序列」同款陷阱，本批实锤踩过一次）。
_BROWSER_TERMS = ("刷新" + "页面", "刷新" + "此面板", "重新整理" + "頁面",
                  "重新整理" + "此面板", "Refresh the " + "page", "refresh to " + "load")

# 只扫「更新提示」这条链的文案值，不扫整文件——同类术语在会话过期/CSRF 链也有
# （cp.persona.fail_auth / inbox.skel.session_expired，2026-08-28 顺带查出、未在本
# 批处理），那是独立工作面，宽口径会把它们连坐进来。
_PROMPT_KEY_RE = re.compile(
    r"""["'](?:ws\.uibuild\.\w+|hb\.stale\.\w+|cp\.app\.stale_\w+)["']\s*:\s*["'](.*?)["'],""")
_TPL_DEFAULT_RE = re.compile(
    r"""i18n\s*or\s*\{\}\)?\.get\(\s*['"](?:ws\.uibuild|hb\.stale)[\w.]*['"]\s*,\s*['"](.*?)['"]\s*\)""")
_TPL_DEFAULT2_RE = re.compile(
    r"""i18n\.get\(\s*['"](?:ws\.uibuild|hb\.stale)[\w.]*['"]\s*,\s*['"](.*?)['"]\s*\)""")
# 副驾条：文案是 HTML 内联兜底（data-cp-i18n 标注 + 元素内文本）
_CP_INLINE_RE = re.compile(r"""data-cp-i18n=["']cp\.app\.stale_\w+["'][^>]*>([^<]*)<""")


def test_no_browser_refresh_wording_in_update_prompts():
    """桌面壳没有刷新按钮：提示只许描述结果（「重新载入」），不许教浏览器操作。"""
    targets = [PACK_WS, PACK_HB, WSB, BASE]
    targets += [t / "app.html" for t in _COPILOT_TREES]
    targets += [t / "i18n" / "cp-i18n.js" for t in _COPILOT_TREES]
    bad = []
    for p in targets:
        src = _read(p)
        values = (_PROMPT_KEY_RE.findall(src) + _TPL_DEFAULT_RE.findall(src)
                  + _TPL_DEFAULT2_RE.findall(src) + _CP_INLINE_RE.findall(src))
        assert values, f"{p.name}: 没抓到任何更新提示文案——扫描器与文件结构脱节了"
        for v in values:
            for term in _BROWSER_TERMS:
                if term in v:
                    bad.append(f"{p.name}: {v[:40]}")
    assert not bad, "更新提示里仍有浏览器操作术语（桌面壳做不到）：\n  " + "\n  ".join(bad)


def test_wording_scanner_catches_regression():
    """探测器自证：旧措辞塞回去必须被抓到（防「永远绿的摆设」）。"""
    old = '    "ws.uibuild.text": "工作台界面已更新到新版本，' + "刷新" + '页面即可加载。",\n'
    vals = _PROMPT_KEY_RE.findall(old)
    assert vals, "扫描器连标准键值行都抓不到"
    assert any(t in vals[0] for t in _BROWSER_TERMS), "扫描器认不出被禁术语"
    cp_old = '<span data-cp-i18n="cp.app.stale_text">副驾界面已更新，' + "刷新" + '此面板即可加载。</span>'
    assert any(t in _CP_INLINE_RE.findall(cp_old)[0] for t in _BROWSER_TERMS)


def test_update_prompt_keys_bilingual():
    src = _read(PACK_WS)
    for key in ("ws.uibuild.short", "ws.uibuild.text", "ws.uibuild.btn",
                "ws.uibuild.unsent", "ws.uibuild.force", "ws.uibuild.later",
                "ws.uibuild.auto_done"):
        assert src.count(f'"{key}"') >= 2, f"{key} 缺 zh/en 之一"
    hb = _read(PACK_HB)
    for key in ("hb.stale.short", "hb.stale.text", "hb.stale.btn"):
        assert hb.count(f'"{key}"') >= 2, f"{key} 缺 zh/en 之一"


def test_copilot_stale_bar_wording_synced_in_both_trees():
    for p, html in _both("app.html"):
        assert "立即更新" in html, f"{p} 副驾条按钮文案未同批更新"
        assert "severity" in html, f"{p} 副驾探针未认 severity（silent 仍会打扰）"


# ── 3) 工作台：常驻按钮 / 未发送保护 / 空闲静默换版 ─────────────────────────
def test_workspace_capsule_carries_reload_action():
    src = _read(WSB).replace(" ", "")
    assert "action:{label:BTN,onClick:_reloadNow" in src, (
        "工作台胶囊必须带常驻「立即更新」按钮（卡片 20s 过期后的唯一出路）")


def test_workspace_guards_unsent_media_before_reload():
    """暂存图片/语音重载即丢，且它们**没有**输入框那样的草稿幸存机制。"""
    src = _read(WSB)
    assert "_blockingUnsent" in src
    assert "media-preview-bar" in src and "voice-preview" in src, (
        "未发送保护必须真的去看媒体队列与语音预览，否则是空壳判断")
    assert "uibuild-unsent" in src, "有未发送媒体时要弹确认卡而不是直接重载"


def test_workspace_idle_auto_reload_present_and_bounded():
    src = _read(WSB)
    assert "_tryAuto" in src and "AUTO_HIDDEN_MS" in src, "缺空闲静默换版"
    assert "AUTO_MAX_PER_H" in src, "自动重载必须有每小时配额上限（防 bump 风暴/自我循环）"
    assert "ws_uibuild_autoed" in src, "自动换版后必须一次性知会，否则坐席不知道发生了什么"


def test_notif_center_ongoing_rows_are_actionable():
    """胶囊点开落到消息中心，那里此前是 cursor:default 死胡同。"""
    src = _read(WSB)
    assert "data-ntf-act" in src, "「进行中」条目缺动作按钮"
    assert "AITRNotify.ongoing.invoke" in src, "动作按钮未接回总线"
    assert "__ogActBound" in src, "事件委托要幂等绑定（每次重绘再绑=按钮点一次触发多回）"


# ── 4) 后台壳：刻意不自动重载（反向钉） ─────────────────────────────────────
def test_admin_shell_has_action_but_deliberately_no_auto_reload():
    src = _read(BASE)
    seg = src[src.find("adm_uibuild_snooze"):]
    seg = seg[:seg.find("</script>")]
    assert "action:{label:BTN, onClick:reloadNow" in seg, "后台壳胶囊也要常驻动作按钮"
    assert "_tryAuto" not in seg and "AUTO_HIDDEN_MS" not in seg, (
        "后台页多是编辑中的长表单，自动重载会丢未保存配置——这是有意的不对称，"
        "别顺手与工作台『统一』")


# ── 5) severity：发布侧写 × 三处探针读 ──────────────────────────────────────
def _bump_mod():
    spec = importlib.util.spec_from_file_location("bump_ui_build", BUMP)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_bump_script_writes_single_severity_line():
    m = _bump_mod()
    assert m.apply_severity(["20260828-1130"], "notice") == [
        "20260828-1130", "severity=notice"]
    # 存量行原地改写，不重复登记、不搬走注释
    got = m.apply_severity(["20260828-1130", "severity=urgent", "# note"], "silent")
    assert got == ["20260828-1130", "severity=silent", "# note"], got
    assert len([ln for ln in got if ln.startswith("severity=")]) == 1


def test_bump_script_keeps_monotonic_guard():
    """并行线刚打了更新的戳时不得回拨（回拨=误触/漏触提示）。"""
    m = _bump_mod()
    assert m.next_stamp("20260828-1200", "20260828-1130") == "20260828-1201"


def test_probes_read_severity_in_all_three_hosts():
    for p in [WSB, BASE] + [t / "app.html" for t in _COPILOT_TREES]:
        assert "severity" in _read(p), f"{p.name} 探针未消费 severity 元数据"
    # silent 档必须真的能让宿主闭嘴（三处各自的判据写法不同，逐个钉）
    assert "sev==='silent'" in _read(WSB).replace(" ", ""), "工作台未实现 silent 档"
    assert "sev==='silent'" in _read(BASE).replace(" ", ""), "后台壳未实现 silent 档"
    for p, html in _both("app.html"):
        assert "silent" in html, f"{p} 未实现 silent 档"


def test_stamp_file_shape_is_backward_compatible():
    """首行永远是构建戳：老页面（未热更新的旧 JS）只读第一行，不能被元数据顶掉。"""
    lines = _read(STAMP).splitlines()
    assert lines and re.match(r"^\d{8}-\d{4}$", lines[0].strip()), (
        f"ui-build.txt 首行必须是构建戳，实际：{lines[:1]}")
    for ln in lines[1:]:
        if ln.strip().startswith("severity="):
            assert ln.strip().split("=", 1)[1] in ("silent", "notice", "urgent")
