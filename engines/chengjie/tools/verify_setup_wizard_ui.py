# -*- coding: utf-8 -*-
"""接入向导「官方渠道深链 + webhook 握手状态条」真浏览器门禁（Playwright；
2026-08-10 随官方渠道开通闭环落地）。

**为什么需要它**：开通闭环的向导侧全是渲染行为——收件箱弹窗带 ?channel= 跳来
必须自动展开该渠道卡+展开官方 API 折叠区+滚动高亮（否则用户对着长列表自己找，
深链等于没接）；「配完凭证不知道通没通」靠 webhook 握手状态条即时翻绿（5s 轮询
+ details 展开即刷）。静态门禁只能证函数在、键在；这些行为只有真浏览器能证明，
而 setup_wizard.html 热更新直上生产。与 tools/verify_connect_modal_ui.py 互为
两半：弹窗侧证「跳出去的 URL 带对参数」，本工具证「带参数进来后页面接得住」。

**夹具模式**：活实例 + token 登录；``/api/setup/channels`` 用真响应（只读 GET，
渠道注册表天然含 instagram/zalo）；``/api/admin/official-webhook-status`` 全程
route-mock（该路由等实例重启装载，mock 使门禁不依赖重启时序，且可注入任意判词
验证状态条分支）。**绝不点保存**（那是真写配置）；上下文级 sendBeacon 桩拦掉
official_setup_* 漏斗埋点（零遥测污染，同 verify_connect_modal_ui 纪律）。

覆盖的不变量：
  W*  源码接线（不依赖实例）：_swDeepLink 消费 ?channel= / hash 回落 /
      展开+高亮+滚动三件套 / sw-hl CSS / REACH_PLATS 四平台 / 状态条判词
      分支齐全 / 保存漏斗埋点 / sw_reach_* i18n zh+en 齐平。
  L1  带 ?channel=instagram 进来 → IG 卡自动展开、官方 API 折叠区自动打开、
      卡片吃到 sw-hl 高亮、滚动进视口。
  L2  握手状态条：mock verdict=live → 条可见且有内容；切 mock 为 auth_failing
      → 下一轮轮询（≤8s）内容随之变化（轮询+重渲染活着，判词驱动 UI）。
  L3  hash 回落：#zalo 进来 → Zalo 卡展开。
  L4  裸进（无参数）→ 不误开任何渠道卡（深链守卫不扰正常浏览）。
  L5  双主题渲染对比度（2026-08-10「暗色看不清」修复的向导半边，口径同
      verify_connect_modal_ui L6）：个人号卡「稳定运行建议」（.sw-login-notice，
      依赖 P3 后端 + web_enabled → 元素不在时 SKIP 不 FAIL）与渠道卡常驻文字在
      light/dark 下实效对比度 ≥4.5；主题经 ?theme= URL 参数（向导页不载
      appearance.js，无夜间模式覆写竞态）。

用法::

    python tools/verify_setup_wizard_ui.py             # 门禁模式
    python tools/verify_setup_wizard_ui.py --headed    # 肉眼看一遍
    python tools/verify_setup_wizard_ui.py --self-proof # 探测器自证

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
WIZARD_TEMPLATE = ENGINE_ROOT / "src" / "web" / "templates" / "setup_wizard.html"

_REACH_KEYS = ("sw_reach_title", "sw_reach_live", "sw_reach_handshake",
               "sw_reach_auth", "sw_reach_not_mounted", "sw_reach_never")


def _reach_payload(verdict: str) -> dict:
    """合成 /api/admin/official-webhook-status 响应（契约见
    unified_inbox_account_routes.api_official_webhook_status docstring）。"""
    return {"ok": True, "platforms": [{
        "platform": "instagram", "verdict": verdict,
        "last_event_age_sec": 42 if verdict == "live" else -1,
        "webhook_path": "/ig/webhook",
    }]}


# ── L5 颜色数学（口径同 verify_connect_modal_ui / verify_theme_contrast_ui；
#     工具独立可跑故内联）──

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

#: (selector, 说明, 必须存在?)——.sw-login-notice 依赖实例状态，缺席=SKIP。
_L5_SPOTS: List[Tuple[str, str, bool]] = [
    (".sw-login-notice", "个人号稳定运行建议", False),
    (".sw-ch-name", "渠道卡标题", True),
    (".sw-ch-intro", "渠道卡简介", True),
]


_STATE_JS = """(chid) => {
  const body = document.getElementById('sw-body-' + chid);
  const card = document.querySelector('.sw-card[data-id="' + chid + '"]');
  const det = body && body.querySelector('details.sw-path-adv');
  const strip = document.querySelector('[data-reach-plat="' + chid + '"]');
  const rect = card ? card.getBoundingClientRect() : null;
  return {
    exists: !!card,
    body_open: !!(body && body.classList.contains('open')),
    api_open: !!(det && det.open),
    highlighted: !!(card && card.classList.contains('sw-hl')),
    in_viewport: !!(rect && rect.top > -60 && rect.top < window.innerHeight),
    strip_visible: !!(strip && strip.style.display !== 'none'
                      && strip.innerHTML.trim().length > 0),
    strip_html: strip ? strip.innerHTML.trim() : '',
    open_bodies: Array.from(document.querySelectorAll('.sw-body.open'))
      .map(b => b.id),
  };
}"""


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

    def summary(self, title: str = "接入向导深链+握手状态条验证") -> int:
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
    print("== W. 源码接线（静态）==")
    ck.check("W1 _swDeepLink 定义且消费 ?channel=",
             "function _swDeepLink" in src
             and bool(re.search(r"URLSearchParams\(location\.search\)\.get\(['\"]channel['\"]\)", src)))
    ck.check("W2 hash 回落（#zalo 这类）",
             bool(re.search(r"location\.hash[\s\S]{0,120}hash\.slice\(1\)", src)))
    ck.check("W3 深链三件套：展开渠道卡 + 展开官方折叠区 + 高亮滚动",
             bool(re.search(r"_swDeepLink[\s\S]{0,900}classList\.add\('open'\)", src))
             and bool(re.search(r"_swDeepLink[\s\S]{0,900}det\.open=true", src))
             and bool(re.search(r"_swDeepLink[\s\S]{0,1100}sw-hl", src))
             and bool(re.search(r"_swDeepLink[\s\S]{0,1300}scrollIntoView", src)))
    ck.check("W4 sw-hl 高亮 CSS 已定义", ".sw-card.sw-hl" in src)
    ck.check("W5 REACH_PLATS 覆盖四官方渠道 + 判词分支齐全",
             bool(re.search(r"REACH_PLATS=\{[^}]*instagram[^}]*zalo[^}]*messenger[^}]*whatsapp", src.replace(" ", "")))
             and all(k in src for k in ("sw_reach_live", "sw_reach_handshake",
                                        "sw_reach_auth", "sw_reach_not_mounted",
                                        "sw_reach_never")))
    ck.check("W6 保存漏斗埋点（official_setup_saved_ 经 ui-event 通道）",
             "official_setup_saved_" in src and "/api/telemetry/ui-event" in src)
    try:
        sys.path.insert(0, str(ENGINE_ROOT))
        from src.web.web_i18n import get_translations
        missing = []
        for lang in ("zh", "en"):
            t = get_translations(lang)
            for key in _REACH_KEYS:
                if not str(t.get(key) or "").strip():
                    missing.append(f"{lang}:{key}")
        ck.check("W7 sw_reach_* i18n zh+en 齐平", not missing, str(missing[:4]))
    except Exception as e:  # noqa: BLE001
        ck.skip("W7 sw_reach_* i18n zh+en 齐平", f"web_i18n 不可导入：{str(e)[:60]}")


def _wait_state(page: Any, chid: str, pred, *, timeout_ms: int = 6000) -> dict:
    deadline = time.time() + timeout_ms / 1000.0
    last: dict = {}
    while time.time() < deadline:
        last = page.evaluate(_STATE_JS, chid)
        try:
            if pred(last):
                return last
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(120)
    return last


def run(base: str, token: str, *, headed: bool = False) -> int:
    ck = Checker()
    src = WIZARD_TEMPLATE.read_text(encoding="utf-8")
    check_source_wiring(ck, src)

    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print("[SKIP] playwright 未安装——只跑静态接线段")
        return ck.summary()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport=VIEWPORT)
        # 零遥测污染：向导保存带 official_setup_saved_ 埋点（本门禁不点保存，
        # 但防御性打桩；将来有人给页面加曝光埋点也不会漏进生产趋势）。
        ctx.add_init_script(
            "try{Object.defineProperty(navigator,'sendBeacon',{value:()=>true})}"
            "catch(_){navigator.sendBeacon=()=>true}")
        try:
            ctx.request.post(base + "/login", form={"auth_token": token})
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP] 实例不可达（{str(e)[:80]}）——只跑静态接线段")
            browser.close()
            return ck.summary()
        page = ctx.new_page()

        holder = {"payload": _reach_payload("live")}

        def _fulfill(route: Any) -> None:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(holder["payload"], ensure_ascii=False))
        page.route("**/api/admin/official-webhook-status*", _fulfill)

        # ── L1 深链：?channel=instagram 自动展开+高亮+滚动 ─────────────────
        print("== L1. ?channel=instagram 深链（真页面 + mock 握手状态）==")
        try:
            page.goto(base + "/workspace/setup?channel=instagram",
                      wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP] 向导页打不开（{str(e)[:80]}）——只跑静态接线段")
            browser.close()
            return ck.summary()
        st = _wait_state(page, "instagram",
                         lambda s: s.get("body_open") and s.get("api_open"))
        ck.check("L1a IG 渠道卡自动展开", st.get("body_open"), str(st.get("open_bodies")))
        ck.check("L1b 官方 API 折叠区自动打开", st.get("api_open"))
        # sw-hl 高亮驻留 2.6s：上面 6s 等待可能已过窗，单独短窗补一拍（先到先得）
        hl = st.get("highlighted") or _wait_state(
            page, "instagram", lambda s: s.get("highlighted"),
            timeout_ms=1500).get("highlighted")
        ck.check("L1c 卡片吃到 sw-hl 高亮", hl)
        ck.check("L1d 卡片滚动进视口", st.get("in_viewport"))

        # ── L2 握手状态条：live 渲染 + 判词切换驱动重渲染 ────────────────────
        print("== L2. 握手状态条（mock live → auth_failing）==")
        st2 = _wait_state(page, "instagram", lambda s: s.get("strip_visible"),
                          timeout_ms=8000)
        ck.check("L2a mock live → 状态条可见有内容", st2.get("strip_visible"),
                 st2.get("strip_html", "")[:60])
        before_html = st2.get("strip_html", "")
        holder["payload"] = _reach_payload("auth_failing")
        st3 = _wait_state(
            page, "instagram",
            lambda s: s.get("strip_visible") and s.get("strip_html") != before_html,
            timeout_ms=9000)
        ck.check("L2b 判词切换 → 下一轮轮询内容更新",
                 st3.get("strip_html") and st3.get("strip_html") != before_html,
                 st3.get("strip_html", "")[:60])

        # ── L3 hash 回落 ────────────────────────────────────────────────────
        print("== L3. #zalo hash 回落 ==")
        page.goto(base + "/workspace/setup#zalo", wait_until="domcontentloaded")
        st4 = _wait_state(page, "zalo", lambda s: s.get("body_open"))
        ck.check("L3a Zalo 渠道卡经 hash 展开", st4.get("body_open"))

        # ── L4 裸进不误开 ───────────────────────────────────────────────────
        print("== L4. 无参数不误开渠道卡 ==")
        page.goto(base + "/workspace/setup", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)   # 等 load() 渲染完
        st5 = page.evaluate(_STATE_JS, "instagram")
        ck.check("L4a 裸进零渠道卡展开", not st5.get("open_bodies"),
                 str(st5.get("open_bodies")))

        # ── L5 双主题渲染对比度（个人号建议卡 + 渠道卡常驻文字）──────────────
        # ?theme= 参数由 workspace_base 头部脚本消费（向导页不载 appearance.js，
        # 无夜间模式覆写竞态——收件箱侧同款断言见 verify_connect_modal_ui L6）。
        print("== L5. 双主题渲染对比度（个人号建议卡 + 渠道卡文字）==")
        for theme in ("light", "dark"):
            page.goto(base + f"/workspace/setup?channel=instagram&theme={theme}",
                      wait_until="domcontentloaded")
            _wait_state(page, "instagram", lambda s: s.get("body_open"))
            page.wait_for_timeout(400)
            samples = page.evaluate(_CONTRAST_JS, [s for s, _, _ in _L5_SPOTS])
            for (sel, label, must), sm in zip(_L5_SPOTS, samples):
                tag = f"L5 [{theme}] {label}"
                if not sm.get("found"):
                    if must:
                        ck.check(f"{tag} ≥4.5", False, f"未找到 {sel}")
                    else:
                        ck.skip(f"{tag} ≥4.5",
                                "元素未渲染（web_enabled 关 / P3 后端未装载）"
                                "——条件满足时自动转实测")
                    continue
                try:
                    ratio = _effective_contrast(
                        sm["color"], sm["bgs"], sm["opacity"])
                except ValueError as e:  # noqa: BLE001
                    ck.check(f"{tag} ≥4.5", False, str(e)[:80])
                    continue
                ck.check(f"{tag} ≥4.5", ratio >= 4.5, f"{ratio:.2f}:1")

        browser.close()
    return ck.summary()


def self_proof() -> int:
    """探测器自证：拆掉深链函数与 channel 参数消费后，W 段必须能红。"""
    src = WIZARD_TEMPLATE.read_text(encoding="utf-8")
    broken = (src
              .replace("function _swDeepLink", "function _zzDeepLink")
              .replace(".get('channel')", ".get('zz')"))
    ck = Checker()
    check_source_wiring(ck, broken)
    fails = [n for n, ok in ck.results if not ok]
    caught = any(n.startswith("W1") for n in fails)
    print(f"\n== self-proof: 破坏后 W1 变红 = {'PASS' if caught else 'FAIL'} "
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
