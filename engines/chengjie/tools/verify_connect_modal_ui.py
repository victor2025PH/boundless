# -*- coding: utf-8 -*-
"""接入弹窗「官方 API 渠道引导态」真浏览器门禁（Playwright；2026-08-10 随
「敬请期待误导」修复落地）。

**为什么需要它**：IG/Zalo 这类纯官方 API 渠道在弹窗里只有一个选项，其
「不可用」形态曾把「差你一步配置」渲染成「敬请期待」（=功能不存在），
说明卡还回落 Telegram 专用指引把人指去一个不存在的表单。修复分三层：
原因码前端登记（chip/说明卡）→ 徽标三态（cfg 蓝=待配置 ≠ soon 灰=没做）→
引导态文案 + 单方式自动展开 + 向导深链。这些全是**渲染行为**：静态门禁
只能证「键在、码在」，证不了「徽标真的蓝、卡真的自动开、点按钮真的带
?channel= 参数」——而模板热更新直上生产。

**夹具模式**：与 tools/verify_inbox_identity.py 同族——活实例 + token 登录 +
``page.route`` 注入合成 modes 响应（**零生产写入**：不真配 IG、不发消息、
不碰会话；打开的向导页只读）。modes 数据全 mock ⇒ 门禁结果与实例的真实
渠道配置状态无关（配没配 IG 都能跑）。**零遥测污染**：上下文级 sendBeacon
桩拦掉 official_setup_* 开通漏斗埋点——否则门禁每周跑一轮＝往运营读的
ui_event_trend 灌假转化事件。

覆盖的不变量（编号对应输出）：
  W*  源码接线（不依赖实例）：码登记两表 / cfg 徽标分支 / 引导态四键 /
      自动展开块 / recheck 免重开向导 / openSetupWizard 深链 / i18n zh+en 齐平。
  L1  mock 单方式不可用（official_creds_missing + field 参数）→ openConnect
      即自动展开引导卡；徽标 .cfg 且文案=「去向导配置」（非「敬请期待」）；
      标题=引导态口吻；🔑 图标；why 含 mock 的字段清单（{field} 插值上屏）；
      how 指向接入向导；alt=「这不是故障」变体。
  L2  「去接入向导」→ 新页 URL 含 /workspace/setup 且 channel=instagram
      （深链契约的弹窗半边；向导侧消费由 _swDeepLink 自己的门禁守）。
  L3  re-mock 为已就绪 → 点「重新检测」→ **不再弹向导标签页**、引导卡收起、
      选项变可点且带「推荐」徽标。
  L4  Zalo 同族冒烟（同一码路，单方式自动展开 + cfg 徽标）。
  L5  switch_off 变体（params.state=switch_off → rc_official_switch_off_* 文案）
      ——弹窗侧消费落地前自动 SKIP（并行线在做；落地后本条自动转实测）。
  L6  双主题渲染对比度（2026-08-10「暗色看不清」修复的回归网）：mock 直供
      两卡布局（个人号推荐+notice / 官方待配置）与 notice.severity（info=终态
      建议皮肤 / warn=历史事故形态，修复前暗色 ≈3:1）——断言与后端重启进度
      解耦；notice 文字、待配置卡与推荐卡描述在 light/dark 下实效对比度 ≥4.5
      （WCAG AA，祖先链合成实底 + 元素级 opacity，口径同 verify_theme_contrast_ui）。
  L7  登录第三步（扫码/托管视图）双主题对比度：状态行 pending/ok/err、LIVE
      徽标、错误详情 ≥4.5。纯 DOM 置类采样（不调 startConnect、零登录会话；
      状态类均为生产可达态）——修复前实测亮色「等待扫码」2.15:1、暗色 LIVE
      徽标 2.36:1（--tk-emerald-ink 未定义恒走亮色回落的同族病）。
  L8  规划中形态（not_implemented，2026-08-11 诚实化）：占位卡沉底、「规划中」
      徽标、沙漏图标、「重新检测」隐藏（永不就绪的安慰剂）、alt 无空头支票、
      「改用 X」直达键可见且点击真的切进可用方式的配置步——修复前该形态渲染
      「填凭据/重新检测」整套指引，用户照做找一个不存在的表单（死胡同实录）。
  L9  运维类卡点行动优先（2026-08-11 P1）：标题行原因短徽（bk_* 上屏）、
      「复制处置指引」可见且点击产出含 {svc}/{url} 参数的可粘贴工单、
      「改用 X」直达键指向可用替代、「重新检测」保留（运维类可重检非安慰剂）。

用法::

    python tools/verify_connect_modal_ui.py             # 门禁模式
    python tools/verify_connect_modal_ui.py --headed    # 肉眼看一遍
    python tools/verify_connect_modal_ui.py --self-proof # 探测器自证

缺 playwright / 实例不可达 → SKIP exit 0（静态接线段照跑）；无 token → exit 2。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}
ENGINE_ROOT = Path(__file__).resolve().parents[1]
INBOX_TEMPLATE = ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"

# 引导态四键 + 徽标兜底键（zh/en 都必须能解析出非空文案）
_CONFIG_KEYS = (
    "inbox.connect.mode_needs_config",
    "inbox.connect.unavail_title_config",
    "inbox.connect.unavail_cue_config",
    "inbox.connect.unavail_alt_config",
    "inbox.connect.bk_official_creds_missing",
)

_CARD_JS = """() => {
  const card = document.getElementById('connect-unavail');
  if (!card) return {absent: true};
  const st = getComputedStyle(card);
  const t = (id) => ((document.getElementById(id) || {}).textContent || '').trim();
  const lock = card.querySelector('.connect-unavail-lock');
  const wiz = document.getElementById('connect-wizard-btn');
  // 图标语义：credentials 引导态=key、其余=lock（2026-08-10 由 emoji 换 uiIcon SVG，
  // 断言按「与注册表产物逐字节一致」判，不再读 textContent）
  // innerHTML 读回的是 DOM 序列化产物（属性序/自闭合都会被规范化），与 uiIcon 原始
  // 字符串不逐字节等 —— 让参照物同样过一次 DOM 往返再比。
  const norm = (s) => { const d = document.createElement('div');
    d.innerHTML = String(s == null ? '' : s); return d.innerHTML.trim(); };
  let lockKind = '';
  if (lock && typeof window.uiIcon === 'function') {
    const html = lock.innerHTML.trim();
    if (html && html === norm(window.uiIcon('key', 15))) lockKind = 'key';
    else if (html && html === norm(window.uiIcon('lock', 15))) lockKind = 'lock';
    else if (html && html === norm(window.uiIcon('hourglass', 15))) lockKind = 'hourglass';
    else if (lock.querySelector('svg')) lockKind = 'svg?';
  }
  const rk = document.getElementById('connect-recheck-btn');
  const ab = document.getElementById('connect-alt-btn');
  const oc = document.getElementById('connect-ops-copy-btn');
  const ch = document.getElementById('connect-unavail-chip');
  return {
    absent: false,
    visible: st.display !== 'none' && card.getBoundingClientRect().height > 0,
    title: t('connect-unavail-title'),
    why: t('connect-unavail-why'),
    how: t('connect-unavail-how'),
    alt: t('connect-unavail-alt'),
    lock: lockKind,
    wizard_visible: !!(wiz && wiz.offsetParent !== null),
    recheck_visible: !!(rk && rk.offsetParent !== null),
    altbtn_visible: !!(ab && ab.offsetParent !== null),
    altbtn_label: ab ? (ab.textContent || '').trim() : '',
    ops_visible: !!(oc && oc.offsetParent !== null),
    chip: (ch && ch.offsetParent !== null) ? (ch.textContent || '').trim() : '',
    chip_warn: !!(ch && ch.classList.contains('warn')),
  };
}"""

_BADGE_JS = """() => {
  const pick = (sel) => {
    const el = document.querySelector(sel);
    return el ? (el.textContent || '').trim() : null;
  };
  return {
    cfg: pick('#connect-modes .connect-mode-badge.cfg'),
    soon: pick('#connect-modes .connect-mode-badge.soon'),
    recommended: pick('#connect-modes .connect-mode-opt.recommended .connect-mode-badge'),
    enabled_opt: !!document.querySelector(
      '#connect-modes .connect-mode-opt:not(.disabled):not(.blocked)'),
    cue: pick('#connect-modes .connect-mode-unavail-cue'),
  };
}"""


def _modes_payload(plat: str, *, available: bool, state: str = "",
                   field: str = "") -> dict:
    """合成 /api/platforms/<plat>/modes 响应（形状对齐 list_modes+_diagnose_modes）。"""
    m: dict = {
        "mode": "official",
        "label": "官方 API 接入",
        "desc": "平台官方开放接口，最合规零封号风险；凭证在接入向导里配置，无需扫码",
        "label_key": "inbox.connect.mode_l_official",
        "desc_key": "inbox.connect.mode_d_official",
        "caps": ["compliant", "light"],
        "login_kind": "credentials",
        "available": available,
        "recommended": True,
        "preferred": bool(available),
        "reason_code": "" if available else "official_creds_missing",
        "reason": "",
        "ready": bool(available),
    }
    if available:
        m["blockers"] = []
    else:
        params: dict = {}
        if state:
            params["state"] = state
        if field:
            params["field"] = field
        m["blockers"] = [{
            "code": "official_creds_missing", "severity": "block",
            **({"params": params} if params else {}),
        }]
    return {"ok": True, "platform": plat, "modes": [m]}


def _modes_payload_planned(plat: str) -> dict:
    """规划中形态（not_implemented）双卡布局＝WhatsApp 的真实形状：web 占位没实现 +
    protocol 可用推荐。web 刻意排**首位**——L8 要证明前端把规划中卡沉底。"""
    web = {
        "mode": "web", "label": "网页扫码",
        "label_key": "inbox.connect.mode_l_web",
        "desc_key": "inbox.connect.mode_d_web",
        "caps": ["compat", "human"], "login_kind": "qr",
        "available": False, "ready": False, "preferred": False,
        "recommended": False,
        "reason_code": "not_implemented", "reason": "",
        "blockers": [{"code": "not_implemented", "severity": "block"}],
    }
    proto = {
        "mode": "protocol", "label": "扫码登录",
        "label_key": "inbox.connect.mode_l_protocol",
        "desc_key": "inbox.connect.mode_d_protocol",
        "caps": ["multi", "light"], "login_kind": "qr",
        "available": True, "ready": True, "preferred": True,
        "recommended": True, "reason_code": "", "reason": "", "blockers": [],
    }
    return {"ok": True, "platform": plat, "modes": [web, proto]}


def _modes_payload_ops(plat: str) -> dict:
    """运维类卡点双卡布局＝WhatsApp sidecar 挂了的真实形状：protocol 可点但必失败
    （available+ready=false=blocked）+ device 可用。L9 断言行动优先三件套。"""
    proto = {
        "mode": "protocol", "label": "扫码登录",
        "label_key": "inbox.connect.mode_l_protocol",
        "desc_key": "inbox.connect.mode_d_protocol",
        "caps": ["multi", "light"], "login_kind": "qr",
        "available": True, "ready": False, "preferred": False,
        "recommended": True, "reason_code": "service_down", "reason": "",
        "blockers": [{"code": "service_down", "severity": "block",
                      "params": {"svc": "whatsapp-baileys",
                                 "url": "http://127.0.0.1:8790"}}],
    }
    device = {
        "mode": "device", "label": "真机 / 模拟器",
        "label_key": "inbox.connect.mode_l_device",
        "desc_key": "inbox.connect.mode_d_device",
        "caps": ["antiban", "need_device"], "login_kind": "device",
        "available": True, "ready": True, "preferred": False,
        "recommended": False, "reason_code": "", "reason": "", "blockers": [],
    }
    return {"ok": True, "platform": plat, "modes": [proto, device]}


def _modes_payload_dual(plat: str, *, severity: str) -> dict:
    """两卡形态（个人号推荐 + 官方待配置）＝IG/Zalo 个人号开闸后的真实布局。

    notice.severity 由 mock 直供 → L6 对比度断言不依赖后端进程装载进度
    （info 终态皮肤在 platform_login 重启生效前就能被钉住）。"""
    web = {
        "mode": "web", "label": "账号登录（个人号）",
        "label_key": "inbox.connect.mode_l_ig_web",
        "desc_key": "inbox.connect.mode_d_ig_web",
        "caps": ["human", "server"], "login_kind": "hosted",
        "available": True, "ready": True, "preferred": True,
        "recommended": True, "blockers": [],
        "notice": {"key": "inbox.connect.notice_unofficial", "severity": severity},
    }
    off = {
        "mode": "official", "label": "官方 API 接入",
        "label_key": "inbox.connect.mode_l_official",
        "desc_key": "inbox.connect.mode_d_official",
        "caps": ["compliant", "light"], "login_kind": "credentials",
        "available": False, "ready": False, "preferred": False,
        "reason_code": "official_creds_missing", "reason": "",
        "blockers": [{"code": "official_creds_missing", "severity": "block"}],
    }
    return {"ok": True, "platform": plat, "modes": [web, off]}


# ── L6 颜色数学（口径同 tools/verify_theme_contrast_ui.py；工具独立可跑故内联）──

def _parse_rgb(s: str) -> Tuple[float, float, float, float]:
    s = s.strip()
    m = re.match(r"rgba?\(([^)]+)\)", s)
    if m:
        parts = [p for p in re.split(r"[,\s/]+", m.group(1)) if p]
        r, g, b = (float(parts[i]) for i in range(3))
        a = float(parts[3]) if len(parts) > 3 else 1.0
        return r, g, b, a
    # color-mix 渍底在 Chromium 的 computed 值可能序列化为 color(srgb r g b / a)
    m = re.match(r"color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)"
                 r"(?:\s*/\s*([\d.]+%?))?\)", s)
    if m:
        r, g, b = (float(m.group(i)) * 255.0 for i in (1, 2, 3))
        raw = m.group(4)
        a = 1.0 if raw is None else (
            float(raw[:-1]) / 100.0 if raw.endswith("%") else float(raw))
        return r, g, b, a
    raise ValueError(f"unparsable color: {s!r}")


def _lum(rgb: Tuple[float, float, float]) -> float:
    def f(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2])


def _effective_contrast(color: str, bg_chain: List[str], opacity: float) -> float:
    """bg_chain=祖先底色 从近到远；末端近不透明（body computed 恒 rgb()）。"""
    base = None
    for spec in reversed(bg_chain):
        r, g, b, a = _parse_rgb(spec)
        if base is None:
            base = (r, g, b)
            continue
        base = tuple(a * ch + (1 - a) * bs for ch, bs in zip((r, g, b), base))
    if base is None:
        base = (255.0, 255.0, 255.0)
    r, g, b, a = _parse_rgb(color)
    a *= max(0.0, min(1.0, opacity))
    fg = tuple(a * ch + (1 - a) * bs for ch, bs in zip((r, g, b), base))
    l1, l2 = _lum(fg), _lum(base)  # type: ignore[arg-type]
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


_CONTRAST_JS = """(selectors) => selectors.map(sel => {
  const el = [...document.querySelectorAll(sel)].find(e => e.offsetParent !== null);
  if (!el) return { sel, found: false };
  const cs = getComputedStyle(el);
  const bgs = []; let op = parseFloat(cs.opacity) || 1; let cur = el;
  while (cur) {
    const s = cur === el ? cs : getComputedStyle(cur);
    if (cur !== el) { const o = parseFloat(s.opacity); if (!isNaN(o)) op *= o; }
    const bg = s.backgroundColor;
    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') bgs.push(bg);
    cur = cur.parentElement;
  }
  return { sel, found: true, color: cs.color, bgs, opacity: op };
})"""


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []
        self.skipped: List[str] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  [SKIP] {name}  {why}")

    def summary(self, title: str = "接入弹窗官方渠道引导态验证") -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== {title}: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def read_token(data_root: str) -> str:
    import yaml
    root = Path(data_root)
    for name in ("config.local.yaml", "config.yaml"):
        fp = root / "config" / name
        if not fp.exists():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


def check_source_wiring(ck: Checker, src: str) -> None:
    """静态接线（不依赖实例；src 可被 --self-proof 换成故意破坏过的副本）。"""
    print("== W. 源码接线（静态）==")
    flat = src.replace(" ", "")
    ck.check("W1 _CONN_REASON_CODES 登记 official_creds_missing",
             "official_creds_missing:1" in flat)
    ck.check("W2 _CONN_BK_CHIP 映射 bk_official_creds_missing",
             "official_creds_missing:'inbox.connect.bk_official_creds_missing'" in flat)
    ck.check("W3 徽标 cfg 分支（credentials 不可用 ≠ 敬请期待）",
             bool(re.search(r"cfgable\?'cfg':'soon'|\(cfgable\?'cfg':'soon'\)", flat))
             and "inbox.connect.mode_needs_config" in src)
    ck.check("W4 单方式 credentials 自动展开引导卡",
             bool(re.search(r"modes\.length===1[\s\S]{0,240}credentials[\s\S]{0,120}showModeUnavailable", src)))
    # 窗宽 400：并行线在条件与收卡之间加了漏斗埋点/庆祝 toast/账号刷新（合流实况），
    # 断言只钉「credentials 就绪分支最终收卡而不是 chooseMode 重开向导」这个行为锚。
    ck.check("W5 recheck 就绪后 credentials 不重开向导",
             bool(re.search(r"_loginKindOf\(_connectPlat,\s*before\)==='credentials'[\s\S]{0,400}hideModeUnavailable", src)))
    ck.check("W6 openSetupWizard 定义且带 ?channel= 深链",
             "function openSetupWizard" in src and "/workspace/setup?channel=" in src)
    ck.check("W7 向导按钮走 openSetupWizard（非裸 window.open）",
             'onclick="openSetupWizard()"' in src)
    ck.check("W8 引导态标题/alt/cue 键已接",
             "inbox.connect.unavail_title_config" in src
             and "inbox.connect.unavail_alt_config" in src
             and "inbox.connect.unavail_cue_config" in src)
    # 规划中形态（not_implemented，2026-08-11）：码进两表 + 三键接线 + 直达键函数
    ck.check("W10 规划中（not_implemented）码登记 + 接线",
             "not_implemented:1" in flat
             and "not_implemented:'inbox.connect.bk_not_implemented'" in flat
             and "inbox.connect.unavail_title_planned" in src
             and "inbox.connect.unavail_cue_planned" in src
             and "function useAltConnectMode" in src)
    # 运维类行动优先（2026-08-11 P1）：复制工单函数已定义且**已挂 window 暴露表**
    # （L8h 实锤：IIFE 内定义漏挂 Object.assign＝死按钮，静态哑按钮门禁在本文件
    # 的超大脚本上曾漏判——这里按暴露表字面量双保险钉住）。
    ck.check("W11 运维类「复制处置指引」接线 + 暴露",
             "function _copyOpsBrief" in src
             and "inbox.connect.ops_copy_btn" in src
             and bool(re.search(r"Object\.assign\(window,\s*\{[\s\S]*?_copyOpsBrief", src)))
    try:
        sys.path.insert(0, str(ENGINE_ROOT))
        from src.web.web_i18n import get_translations
        missing = []
        for lang in ("zh", "en"):
            t = get_translations(lang)
            for key in _CONFIG_KEYS:
                if not str(t.get(key) or "").strip():
                    missing.append(f"{lang}:{key}")
        ck.check("W9 引导态 i18n zh+en 齐平", not missing, str(missing[:4]))
    except Exception as e:  # noqa: BLE001
        ck.skip("W9 引导态 i18n zh+en 齐平", f"web_i18n 不可导入：{str(e)[:60]}")


def _login_workspace(p: Any, base: str, token: str, *, headed: bool):
    browser = p.chromium.launch(headless=not headed)
    ctx = browser.new_context(viewport=VIEWPORT)
    # 上下文级 sendBeacon 桩（覆盖弹窗页 + L2 弹出的向导页）：引导卡/跳向导/就绪确认
    # 都带 official_setup_* 漏斗埋点，门禁每跑一轮就会往生产 ui_event_trend 灌假转化
    # ——运营按这个趋势读「开通漏斗哪步流失」，必须零污染（与 fixture 系工具同纪律）。
    ctx.add_init_script(
        "try{Object.defineProperty(navigator,'sendBeacon',{value:()=>true})}"
        "catch(_){navigator.sendBeacon=()=>true}")
    ctx.request.post(base + "/login", form={"auth_token": token})
    page = ctx.new_page()
    page.goto(base + "/workspace", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof window.openConnect === 'function'", timeout=20000)
    page.wait_for_timeout(1200)
    return browser, ctx, page


def _route_modes(page: Any, plat: str, holder: dict) -> None:
    """按 holder['payload'] 应答 modes（含 ?recheck=1）；holder 可换内容实现二态。"""
    def _fulfill(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(holder["payload"], ensure_ascii=False))
    page.route(f"**/api/platforms/{plat}/modes*", _fulfill)


def _wait_card(page: Any, *, timeout_ms: int = 8000) -> dict:
    deadline = time.time() + timeout_ms / 1000.0
    last: dict = {"absent": True}
    while time.time() < deadline:
        last = page.evaluate(_CARD_JS)
        if last.get("visible"):
            return last
        page.wait_for_timeout(150)
    return last


def run(base: str, token: str, *, headed: bool = False) -> int:
    ck = Checker()
    src = INBOX_TEMPLATE.read_text(encoding="utf-8")
    check_source_wiring(ck, src)

    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print("[SKIP] playwright 未安装——只跑静态接线段")
        return ck.summary()

    with sync_playwright() as p:
        try:
            browser, ctx, page = _login_workspace(p, base, token, headed=headed)
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP] 实例不可达 / 工作台未就绪（{str(e)[:80]}）——只跑静态接线段")
            return ck.summary()

        field_mock = "IG 专业账号 ID / Page Access Token / App Secret / Verify Token"
        holder = {"payload": _modes_payload("instagram", available=False, field=field_mock)}
        _route_modes(page, "instagram", holder)

        # ── L1 单方式不可用：自动展开引导卡 + cfg 徽标 + 引导态文案 ─────────
        print("== L1. IG 未配置：自动展开引导卡（route mock，零生产写入）==")
        page.evaluate("() => openConnect('instagram')")
        card = _wait_card(page)
        ck.check("L1a 引导卡自动展开（无需点击）", card.get("visible"), str(card)[:100])
        badges = page.evaluate(_BADGE_JS)
        ck.check("L1b 徽标类为 .cfg 且非「敬请期待」",
                 badges.get("cfg") and "敬请期待" not in (badges.get("cfg") or "")
                 and "Coming soon" not in (badges.get("cfg") or ""),
                 f"cfg={badges.get('cfg')!r} soon={badges.get('soon')!r}")
        ck.check("L1c 徽标文案=处置短语（去向导配置/Configure in wizard）",
                 (badges.get("cfg") or "") in ("去向导配置", "Configure in wizard"))
        ck.check("L1d 标题为引导态口吻（还差官方凭证/Just add）",
                 ("还差官方凭证" in card.get("title", ""))
                 or ("Just add" in card.get("title", "")))
        ck.check("L1e 头部图标=key（引导态语义，uiIcon SVG）",
                 card.get("lock") == "key", f"lock={card.get('lock')!r}")
        ck.check("L1f why 含 mock 字段清单（{field} 插值上屏）",
                 "Page Access Token" in card.get("why", ""), card.get("why", "")[:90])
        ck.check("L1g how 指向接入向导",
                 ("接入向导" in card.get("how", "")) or ("Setup Wizard" in card.get("how", "")))
        ck.check("L1h alt=「这不是故障」引导变体",
                 ("这不是故障" in card.get("alt", ""))
                 or ("Nothing is broken" in card.get("alt", "")))
        ck.check("L1i 「去接入向导」按钮可见", card.get("wizard_visible"))

        # ── L2 深链契约：新页 URL 带 ?channel=instagram ─────────────────────
        print("== L2. 去接入向导：?channel= 深链 ==")
        try:
            with ctx.expect_page(timeout=6000) as pi:
                page.click("#connect-wizard-btn")
            pop = pi.value
            pop.wait_for_load_state("domcontentloaded", timeout=8000)
            url = pop.url
            ck.check("L2a 新页为接入向导", "/workspace/setup" in url, url)
            ck.check("L2b URL 带 channel=instagram", "channel=instagram" in url, url)
            pop.close()
        except Exception as e:  # noqa: BLE001
            ck.check("L2a 新页为接入向导", False, f"popup 未出现：{str(e)[:70]}")

        # ── L3 就绪后重新检测：不重开向导、卡收起、选项点亮 ──────────────────
        print("== L3. 配好后重新检测：免重开向导 ==")
        holder["payload"] = _modes_payload("instagram", available=True)
        pages_before = len(ctx.pages)
        page.click("#connect-recheck-btn")
        page.wait_for_timeout(1800)
        card3 = page.evaluate(_CARD_JS)
        badges3 = page.evaluate(_BADGE_JS)
        ck.check("L3a 未弹出新标签页", len(ctx.pages) == pages_before,
                 f"pages {pages_before}->{len(ctx.pages)}")
        ck.check("L3b 引导卡已收起", not card3.get("visible"))
        ck.check("L3c 选项变可点且带「推荐」徽标",
                 badges3.get("enabled_opt") and badges3.get("recommended"),
                 f"rec={badges3.get('recommended')!r}")

        # ── L4 Zalo 同族冒烟（同一码路对第二个纯官方渠道成立）───────────────
        print("== L4. Zalo 同族冒烟 ==")
        page.evaluate("() => closeConnect()")
        zholder = {"payload": _modes_payload(
            "zalo", available=False, field="OA Access Token")}
        _route_modes(page, "zalo", zholder)
        page.evaluate("() => openConnect('zalo')")
        card4 = _wait_card(page)
        badges4 = page.evaluate(_BADGE_JS)
        ck.check("L4a Zalo 自动展开引导卡", card4.get("visible"))
        ck.check("L4b Zalo cfg 徽标（非敬请期待）",
                 badges4.get("cfg") and "敬请期待" not in (badges4.get("cfg") or ""))

        # ── L5 switch_off 变体（并行线弹窗侧消费落地后自动转实测）────────────
        if "switch_off" in src:
            print("== L5. switch_off 变体（凭证齐、只差开关）==")
            page.evaluate("() => closeConnect()")
            zholder["payload"] = _modes_payload(
                "zalo", available=False, state="switch_off")
            page.evaluate("() => openConnect('zalo')")
            card5 = _wait_card(page)
            ck.check("L5a switch_off 走专属文案（只差渠道启用开关）",
                     ("只差渠道启用开关" in card5.get("why", ""))
                     or ("switch" in card5.get("why", "").lower()),
                     card5.get("why", "")[:80])
        else:
            ck.skip("L5 switch_off 变体", "弹窗侧尚未消费 params.state（并行线在做）——落地后本条自动转实测")

        # ── L6 双主题渲染对比度（2026-08-10「暗色看不清」修复的回归网）──────────
        # 主题切换必须在页面启动**后**置 cp_theme + cpApplyTheme：appearance.js
        # 引擎 boot 会按坐席夜间偏好回写 cp_theme，boot 前注入会被覆写
        # （URL ?theme= 已另有 URL_THEME_PIN 兜底，此处走通用路径以贴近真实坐席）。
        print("== L6. 双主题渲染对比度（info/warn 皮肤 + 待配置卡/推荐卡描述）==")
        l6_spots = [
            (".connect-mode-notice", "notice 文字"),
            (".connect-mode-opt.disabled .connect-mode-desc", "待配置卡描述"),
            (".connect-mode-opt.recommended .connect-mode-desc", "推荐卡描述"),
        ]
        for theme in ("light", "dark"):
            page.evaluate(
                "(t) => { localStorage.setItem('cp_theme', t);"
                " if (window.cpApplyTheme) window.cpApplyTheme(); }", theme)
            page.wait_for_timeout(250)
            for sev in ("info", "warn"):
                page.evaluate("() => closeConnect()")
                holder["payload"] = _modes_payload_dual("instagram", severity=sev)
                page.evaluate("() => openConnect('instagram')")
                try:
                    page.wait_for_selector(".connect-mode-notice", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(300)
                # 卡描述不随 severity 变，只在 info 轮采样一次防重复计数
                spots = l6_spots if sev == "info" else l6_spots[:1]
                samples = page.evaluate(_CONTRAST_JS, [s for s, _ in spots])
                for (sel, label), sm in zip(spots, samples):
                    tag = f"L6 [{theme}/{sev}] {label}"
                    if not sm.get("found"):
                        ck.check(f"{tag} ≥4.5", False, f"未找到 {sel}")
                        continue
                    try:
                        ratio = _effective_contrast(
                            sm["color"], sm["bgs"], sm["opacity"])
                    except ValueError as e:
                        ck.check(f"{tag} ≥4.5", False, str(e)[:80])
                        continue
                    ck.check(f"{tag} ≥4.5", ratio >= 4.5, f"{ratio:.2f}:1")

        # ── L7 登录第三步（扫码/托管）双主题对比度 ──────────────────────────
        # 纯 DOM 置类（不调 startConnect，零登录会话创建）；状态类均生产可达。
        print("== L7. 登录第三步双主题对比度（状态行/LIVE 徽标/错误详情）==")
        _qr_setup = """() => {
          document.getElementById('connect-mode-view').style.display = 'none';
          document.getElementById('connect-config-view').style.display = 'none';
          document.getElementById('connect-qr-view').style.display = 'block';
          const wrap = document.querySelector('.connect-qr-wrap');
          if (wrap) wrap.classList.add('hosted', 'has-img');
          const ed = document.getElementById('connect-err-detail');
          if (ed) ed.style.display = 'block';
          const eb = document.getElementById('connect-err-body');
          if (eb) { eb.style.display = 'block'; eb.textContent = 'RPCError: sample'; }
        }"""
        for theme in ("light", "dark"):
            page.evaluate(
                "(t) => { localStorage.setItem('cp_theme', t);"
                " if (window.cpApplyTheme) window.cpApplyTheme(); }", theme)
            page.evaluate("() => closeConnect()")
            page.evaluate("() => openConnect('instagram')")
            page.wait_for_timeout(350)
            page.evaluate(_qr_setup)
            page.wait_for_timeout(200)
            for st_cls in ("pending", "ok", "err"):
                page.evaluate(
                    "(v) => { const el = document.getElementById('connect-state');"
                    " el.className = 'connect-state ' + v; el.textContent = v; }",
                    st_cls)
                sm = page.evaluate(_CONTRAST_JS, ["#connect-state"])[0]
                tag = f"L7 [{theme}] 状态行.{st_cls} ≥4.5"
                if not sm.get("found"):
                    ck.check(tag, False, "状态行未渲染")
                    continue
                ratio = _effective_contrast(sm["color"], sm["bgs"], sm["opacity"])
                ck.check(tag, ratio >= 4.5, f"{ratio:.2f}:1")
            for sel, label in ((".connect-live-badge", "LIVE 徽标"),
                               ("#connect-err-body", "错误详情")):
                sm = page.evaluate(_CONTRAST_JS, [sel])[0]
                tag = f"L7 [{theme}] {label} ≥4.5"
                if not sm.get("found"):
                    ck.check(tag, False, f"未找到 {sel}")
                    continue
                ratio = _effective_contrast(sm["color"], sm["bgs"], sm["opacity"])
                ck.check(tag, ratio >= 4.5, f"{ratio:.2f}:1")

        # ── L8 规划中形态（not_implemented，2026-08-11 诚实化）────────────────
        # 静态门禁证不了「沙漏真的渲染、重新检测真的藏了、直达键真的能切走」——
        # 而这正是「照指引找一个不存在的表单」那类死胡同的行为面。
        print("== L8. 规划中形态：沉底 + 沙漏 + 藏重检 + 一键切换 ==")
        page.evaluate(
            "(t) => { localStorage.setItem('cp_theme', t);"
            " if (window.cpApplyTheme) window.cpApplyTheme(); }", "light")
        page.evaluate("() => closeConnect()")
        wholder = {"payload": _modes_payload_planned("whatsapp")}
        _route_modes(page, "whatsapp", wholder)
        page.evaluate("() => openConnect('whatsapp')")
        page.wait_for_selector("#connect-modes .connect-mode-opt", timeout=8000)
        page.wait_for_timeout(250)
        order = page.evaluate(
            "() => [...document.querySelectorAll('#connect-modes .connect-mode-opt')]"
            ".map(el => ((el.querySelector('.connect-mode-name')||{}).textContent||'').trim())")
        ck.check("L8a 规划中卡沉底（payload 首位 → 渲染末位）",
                 len(order) == 2 and ("网页扫码" in order[-1] or "Web QR" in order[-1]),
                 str(order))
        badges8 = page.evaluate(_BADGE_JS)
        ck.check("L8b 徽标=「规划中/Planned」",
                 (badges8.get("soon") or "") in ("规划中", "Planned"),
                 f"soon={badges8.get('soon')!r}")
        # aria-disabled 卡会被 Playwright 可点性检查拒掉（真实点击不受影响）——
        # 走 DOM 派发，与行内 onclick/键盘路径同语义。
        page.evaluate(
            "() => document.querySelector('#connect-modes .connect-mode-opt.disabled').click()")
        card8 = _wait_card(page)
        ck.check("L8c 标题=「还未上线」口吻（非『暂时不能用』）",
                 ("还未上线" in card8.get("title", ""))
                 or ("shipped" in card8.get("title", "")),
                 card8.get("title", "")[:60])
        ck.check("L8d 头部图标=hourglass（沙漏，非锁）",
                 card8.get("lock") == "hourglass", f"lock={card8.get('lock')!r}")
        ck.check("L8e 「重新检测」已隐藏（永不就绪的安慰剂按钮）",
                 not card8.get("recheck_visible"))
        ck.check("L8f 空头支票已消失（alt 不再许『就绪后自动可选』）",
                 "自动可选" not in card8.get("alt", "")
                 and "unlocks automatically" not in card8.get("alt", ""),
                 card8.get("alt", "")[:60])
        ck.check("L8g 「改用 X」直达键可见且带替代方式名",
                 card8.get("altbtn_visible")
                 and (("扫码登录" in card8.get("altbtn_label", ""))
                      or ("Scan" in card8.get("altbtn_label", ""))),
                 f"label={card8.get('altbtn_label')!r}")
        page.click("#connect-alt-btn")
        page.wait_for_timeout(400)
        stepped = page.evaluate(
            "() => { const v = document.getElementById('connect-config-view');"
            " return !!(v && getComputedStyle(v).display !== 'none'); }")
        ck.check("L8h 点直达键真的切进可用方式（配置步已展开）", stepped)

        # ── L9 运维类卡点行动优先（2026-08-11 P1）：chip + 复制工单 + 改用直达 ────
        # sidecar 挂了坐席自己修不了——说明卡必须给出「递话给能修的人」与「换条能走
        # 的路」两个真行动，而不是一堵只有「重新检测」的文字墙。
        print("== L9. 运维类卡点：原因短徽 + 复制处置指引 + 改用直达 ==")
        page.evaluate("() => closeConnect()")
        oholder = {"payload": _modes_payload_ops("whatsapp")}
        _route_modes(page, "whatsapp", oholder)
        page.evaluate("() => openConnect('whatsapp')")
        page.wait_for_selector("#connect-modes .connect-mode-opt", timeout=8000)
        page.wait_for_timeout(250)
        page.evaluate(
            "() => document.querySelector('#connect-modes .connect-mode-opt.blocked').click()")
        card9 = _wait_card(page)
        ck.check("L9a 原因短徽=「服务未运行」上标题行",
                 ("服务未运行" in card9.get("chip", ""))
                 or ("Service not running" in card9.get("chip", "")),
                 f"chip={card9.get('chip')!r}")
        ck.check("L9b 「复制处置指引」按钮可见", card9.get("ops_visible"))
        ck.check("L9c 「改用 X」直达键指向可用的真机方式",
                 card9.get("altbtn_visible")
                 and (("真机" in card9.get("altbtn_label", ""))
                      or ("evice" in card9.get("altbtn_label", ""))),
                 f"label={card9.get('altbtn_label')!r}")
        ck.check("L9d 「重新检测」保留（运维类可重检，非安慰剂）",
                 card9.get("recheck_visible"))
        page.evaluate(
            "() => { navigator.clipboard.writeText = (t) =>"
            " { window.__opsBrief = t; return Promise.resolve(); }; }")
        page.click("#connect-ops-copy-btn")
        page.wait_for_timeout(300)
        brief = page.evaluate("() => window.__opsBrief || ''")
        ck.check("L9e 工单文本含服务名+地址参数（可直接照做）",
                 "whatsapp-baileys" in brief and "127.0.0.1:8790" in brief,
                 brief[:80].replace("\n", " "))

        browser.close()
    return ck.summary()


def self_proof() -> int:
    """探测器自证：把登记表/深链故意破坏后，W 段必须能红。"""
    src = INBOX_TEMPLATE.read_text(encoding="utf-8")
    broken = (src
              .replace("official_creds_missing:1", "zz_creds:1")
              .replace("/workspace/setup?channel=", "/workspace/setup?x="))
    ck = Checker()
    check_source_wiring(ck, broken)
    fails = [n for n, ok in ck.results if not ok]
    caught = any(n.startswith("W1") for n in fails) and any(
        n.startswith("W6") for n in fails)
    print(f"\n== self-proof: 破坏后 W1/W6 变红 = {'PASS' if caught else 'FAIL'} "
          f"(fails={fails})")
    return 0 if caught else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--self-proof", action="store_true")
    args = ap.parse_args()

    if args.self_proof:
        return self_proof()

    token = read_token(args.data_root)
    if not token:
        print("ABORT: 读不到 web_admin.auth_token（--data-root 指对实例数据根）")
        return 2
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
