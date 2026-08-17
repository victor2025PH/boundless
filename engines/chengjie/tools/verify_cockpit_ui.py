# -*- coding: utf-8 -*-
"""驾驶舱页 真浏览器门禁（Playwright；P0/P1 改版 2026-08-14 随批落地）。

**为什么需要它**：cockpit.html 的核心行为全是纯前端交互——KPI 由队列快照推导、
>3 天旧信号折叠进「历史积压」（接管超时豁免）、分类筛选 chips、caps 特性探测
（旧后端无 resolve 端点 → 不渲染「已处理」按钮）、空态 ROI 行、会话可读名兜底。
静态门禁只能证「函数挂了 window / id 不重复」，证不了「旧后端真的不出按钮、
积压卡真的被折走、筛选真的滤得动」，而模板热更新直上生产。

**route-mock 模式**（与 ``tools/verify_inbox_identity.py`` 同族）：真实例 + token
登录拿到完整页面（含 workspace_base 全套），但驾驶舱的数据面
（overview / fleet-health / ai-weekly-brief / resolve）全部经 ``page.route``
注入合成响应——**零生产写入**（resolve POST 被拦在浏览器层，不落库）、
**零遥测污染**（sendBeacon 打桩成记录器，ck_* 漏斗不灌水）、数据确定性
（断言不依赖生产队列当时长什么样）。

覆盖的不变量（编号对应 run() 里的断言组）：
  0. 源码接线（不依赖实例）：caps 守卫 / 积压豁免 / 筛选自愈 / ROI 行 / 埋点在源码里。
  1. KPI 单口径：需介入=新鲜卡数（>72h 的 needs_human 被折走、同龄接管超时豁免留守）、
     历史积压=折叠数、最久等待取新鲜区最大值。
  2. 首次引导条可见 → 「知道了」→ 隐藏 → 刷新后仍隐藏（localStorage 记忆）。
  3. 分类筛选：chips 带计数；点单类只剩该类卡；点「全部」恢复。
  4. 历史积压折叠区：默认收起（卡不可见）→ 点开 → 渲染积压卡。
  5. 「已处理」按钮：caps.resolve=true 且卡含 needs_human 才渲染；点击（确认桩放行）
     → POST /api/cockpit/resolve 携带正确 conversation_id（拦截层收到，不落生产）。
  6. 特性探测降级：overview 不带 caps（旧后端形态）→ 刷新后全页零「已处理」按钮。
  7. 空态：items=[] → 「AI 在岗」空态 + ROI 行（ai-weekly-brief 桩出 57）渲染。
  8. 信号源降级警示：sources 含 error → 顶部警示行可见。
  9. 会话可读名兜底：裸 id 项显示「客户（尾号 xxxx）」/@username，原始 cid 不上屏。
 10. 接管联动：接管在场的会话卡按钮=「交还 AI」；右栏在场行渲染。
 11. 身份卡（P3）：头像渲染 / 原话引用（客户说/AI 草稿前缀）/ 接管卡 detail=接管人
     归 why 行不冒充原话 / 平台品牌名；resolve 后喂料确认 → POST /api/learner/feed。
 11b. 头像代理懒加载（P4）：无直链的 telegram 卡经 /api/platforms/*/avatar 桩
     加载出 blob 头像；代理 404（whatsapp）与坏直链（404 png）都自摘留字母底。
 12. 账号健康折叠：状态未知行收进「另有 N 个」折叠行，点开展开；
     空态学习队列引流行（pending>0 才渲染）。

用法::

    python tools/verify_cockpit_ui.py             # 门禁模式
    python tools/verify_cockpit_ui.py --headed    # 肉眼看一遍

**副作用：无。** 缺 playwright / 实例不可达 → SKIP exit 0；无 token → ABORT exit 2。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1360, "height": 900}
ENGINE_ROOT = Path(__file__).resolve().parents[1]
COCKPIT_TEMPLATE = ENGINE_ROOT / "src" / "web" / "templates" / "cockpit.html"

NOW = time.time()

# ── 合成快照（形态全覆盖：四类卡 + 积压 + 接管豁免 + 裸 id + 源降级 + 身份卡）─
_ITEMS_FULL: List[Dict[str, Any]] = [
    # 接管超时：age>72h 但必须豁免折叠（3 天没交还是最该炸的，不是积压）；
    # detail=接管人（并进 why 行），客户原话走 last_text（P3 补齐字段）。
    # P4：无 avatar_url 但带寻址三元组 → 必须经代理端点懒加载（桩回 PNG → blob 头像）
    {"kind": "takeover_overdue", "priority": 1, "conversation_id": "telegram:a:tko1",
     "name": "", "platform": "telegram", "age_sec": 90000.0, "detail": "agentA",
     "last_text": "麻烦尽快发货", "flags": ["takeover_overdue"], "unread": 0,
     "account_id": "a", "chat_key": "tko1", "chat_type": "private"},
    # needs_human：带原话 + 坏头像 URL（加载失败必须露出字母底，不裂图）
    {"kind": "needs_human", "priority": 2, "conversation_id": "telegram:a:nh1",
     "name": "客户甲", "platform": "telegram", "age_sec": 7200.0,
     "detail": "我要退款，等了很久了", "avatar_url": "/static/__ck_gate_nope.png",
     "username": "kehu_jia", "flags": ["needs_human"], "unread": 2},
    # 裸 id（name==cid、无 username）：显示层必须兜底成「客户（尾号 xxxx）」。
    # P4：代理桩回 404（无头像）→ img 自摘、字母底留守不裂图。
    # P5：detail 空 + quote_media=photo（纯媒体末条）→ 引用气泡渲染「[图片]」占位
    {"kind": "waiting", "priority": 3, "conversation_id": "whatsapp:b:88997766",
     "name": "whatsapp:b:88997766", "platform": "whatsapp", "age_sec": 3600.0,
     "detail": "", "quote_media": "photo", "flags": ["waiting"], "unread": 1,
     "account_id": "b", "chat_key": "88997766", "chat_type": "private"},
    # 裸 id 但有 username：显示 @username（好过尾号）
    {"kind": "draft_pending", "priority": 4, "conversation_id": "line:c:d1",
     "name": "line:c:d1", "username": "hao123", "platform": "line",
     "age_sec": 600.0, "detail": "草稿摘要…", "flags": ["draft_pending"],
     "unread": 0},
    # 陈年 needs_human（>72h）：必须被折进「历史积压」
    {"kind": "needs_human", "priority": 2, "conversation_id": "telegram:a:old1",
     "name": "陈年客户", "platform": "telegram", "age_sec": 400000.0, "detail": "",
     "flags": ["needs_human"], "unread": 0},
]

def _overview(mode: str) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "ok": True,
        "generated_at": NOW,
        "cache_age_sec": 0.0,
        "counts": {},
        "sources": {"takeover_overdue": "ok", "needs_human": "ok",
                    "waiting": "error", "draft_pending": "ok"},
        "takeover": {
            "active": [{"conversation_id": "telegram:a:tko1", "by": "agentA",
                        "since": NOW - 5400, "elapsed_sec": 5400.0}],
            "stats": {"started": 3, "active": 1, "avg_duration_sec": 600.0},
        },
    }
    if mode == "empty":
        base["items"] = []
        base["takeover"] = {"active": [], "stats": {}}
        base["sources"] = {k: "ok" for k in base["sources"]}
        base["caps"] = {"resolve": True}
    elif mode == "nocaps":            # 旧后端形态：无 caps 键
        base["items"] = json.loads(json.dumps(_ITEMS_FULL))
    else:                             # full
        base["items"] = json.loads(json.dumps(_ITEMS_FULL))
        base["caps"] = {"resolve": True}
    return base


_FLEET = {"accounts": [
    {"platform": "telegram", "account_id": "a1", "label": "主号", "status": "active"},
    {"platform": "whatsapp", "account_id": "b1", "label": "备号", "status": "banned"},
    {"platform": "line", "account_id": "c1", "label": "三号", "status": "connecting"},
]}


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。"""
    import yaml
    root = Path(data_root)
    for name in ("config.local.yaml", "config.yaml"):
        fp = root / "config" / name
        if not fp.exists():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
              + (f"  {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        print(f"\n== 驾驶舱页验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def check_source_wiring(ck: Checker) -> None:
    """不依赖实例：改版核心不变量必须还在源码里（防「为省事」删守卫的静默回归）。"""
    print("== 0. 源码接线（静态）==")
    if not COCKPIT_TEMPLATE.exists():
        ck.check("cockpit.html 存在", False, str(COCKPIT_TEMPLATE))
        return
    src = COCKPIT_TEMPLATE.read_text(encoding="utf-8")
    ck.check("caps 特性探测守卫在（_caps.resolve）", "_caps.resolve" in src)
    ck.check("积压豁免：接管超时永远新鲜", "takeover_overdue" in src and "isStale" in src)
    ck.check("筛选自愈（类别清空自动回全部）", "if(_filter && !counts[_filter])" in src)
    ck.check("空态 ROI 行接线", "ck-empty-roi" in src and "ai-weekly-brief" in src)
    ck.check("埋点前缀 ck_ 在", "ck_filter_" in src and "ck_resolve" in src)
    ck.check("可读名兜底函数在", "function displayName" in src)
    ck.check("头像渲染函数在（字母底+坏图自摘）", "function avaHtml" in src
             and "onerror" in src)
    ck.check("人话时长在（L.ageD 词条链）", "L.ageD" in src and "L.ageH" in src)
    ck.check("学习队列打通（resolve 喂料 + 空态引流）",
             "/api/learner/feed" in src and "/api/learner/stats" in src)
    ck.check("头像代理受控加载在（_avProxyUrl + data-av 队列）",
             "_avProxyUrl" in src and "data-av" in src and "_avPump" in src)


_STATE_JS = """() => {
  const $ = (id) => document.getElementById(id);
  const txt = (id) => (($(id) || {}).textContent || '').trim();
  const vis = (id) => { const el = $(id); return !!el && el.offsetParent !== null; };
  const cards = (rootId) => Array.from(
    (document.getElementById(rootId) || document.createElement('i'))
      .querySelectorAll('.ck-card'));
  const cardInfo = (c) => ({
    nm: ((c.querySelector('.nm') || {}).textContent || '').trim(),
    kinds: Array.from(c.querySelectorAll('.ck-kind')).map(k => k.textContent.trim()),
    btns: Array.from(c.querySelectorAll('.ck-btn')).map(b => b.textContent.trim()),
    plat: ((c.querySelector('.ck-plat') || {}).textContent || '').trim(),
    quote: ((c.querySelector('.dt') || {}).textContent || '').trim(),
    hasAva: !!c.querySelector('.ck-ava'),
    avaSrc: ((c.querySelector('.ck-ava img') || {}).src || ''),
    why: ((c.querySelector('.why') || {}).textContent || '').trim(),
  });
  return {
    kQueue: txt('ck-k-queue'), kTakeover: txt('ck-k-takeover'),
    kOldest: txt('ck-k-oldest'), kStale: txt('ck-k-stale'),
    hintVisible: vis('ck-hint'),
    chips: Array.from(document.querySelectorAll('#ck-fchips .ck-fchip'))
      .map(c => ({ tx: c.textContent.trim(), on: c.classList.contains('on') })),
    mainCards: cards('ck-q-list').map(cardInfo),
    staleHeaderVisible: vis('ck-stale-h'),
    staleHeaderText: txt('ck-stale-t'),
    staleListVisible: vis('ck-stale-list'),
    staleCards: cards('ck-stale-list').map(cardInfo),
    srcWarnVisible: vis('ck-srcwarn'),
    emptyBig: (document.querySelector('#ck-q-list .ck-empty .big') || {}).textContent || '',
    roiText: txt('ck-empty-roi'),
    railTk: txt('ck-rail-tk'),
    acctRows: document.querySelectorAll('#ck-rail-acct .ck-acct-row').length,
    acctSum: txt('ck-acct-sum'),
    acctMore: ((document.querySelector('#ck-rail-acct .ck-acct-more') || {}).textContent || '').trim(),
    acctUnVisible: vis('ck-acct-un'),
    learnText: txt('ck-empty-learn'),
    bodyHasRawCid: document.body.innerText.indexOf('whatsapp:b:88997766') >= 0,
  };
}"""


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    check_source_wiring(ck)

    state = {"mode": "full"}
    resolve_posts: List[Dict[str, Any]] = []
    feed_posts: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport=VIEWPORT)
        # 文案断言按 zh 词条写 → 语言钉死（实例默认语言可能被运营改成 en/vi）
        from urllib.parse import urlparse
        _host = urlparse(base).hostname or "127.0.0.1"
        ctx.add_cookies([{"name": "ui_lang", "value": "zh",
                          "domain": _host, "path": "/"}])
        ctx.request.post(base + "/login", form={"auth_token": token})
        page = ctx.new_page()

        # 遥测打桩：ck_* 漏斗不灌水，同时留存记录供断言
        page.add_init_script(
            "window.__beacons=[];"
            "navigator.sendBeacon=function(u,b){window.__beacons.push(String(u));return true;};")

        # ── 数据面全拦截（生产零读扰动、零写入）───────────────────────────
        def _route_overview(route: Any) -> None:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(_overview(state["mode"])))

        def _route_resolve(route: Any) -> None:
            try:
                resolve_posts.append(json.loads(route.request.post_data or "{}"))
            except Exception:
                resolve_posts.append({"__bad": True})
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "removed": True}))

        def _route_feed(route: Any) -> None:
            try:
                feed_posts.append(json.loads(route.request.post_data or "{}"))
            except Exception:
                feed_posts.append({"__bad": True})
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True}))

        page.route("**/api/cockpit/overview*", _route_overview)
        page.route("**/api/cockpit/resolve", _route_resolve)
        page.route("**/api/accounts/fleet-health*", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_FLEET)))
        page.route("**/api/workspace/ai-weekly-brief*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "available": True, "sent": 57})))
        # 学习队列（P3 打通）：stats=可用+4 条待审草稿；feed 拦截层收 payload
        page.route("**/api/learner/stats*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"available": True, "pending": 4})))
        page.route("**/api/learner/feed", _route_feed)
        # 坏头像 URL：确定性 404（验证 onerror 自摘 → 字母底不裂图）
        page.route("**/static/__ck_gate_nope.png", lambda r: r.fulfill(status=404))
        # P4 头像代理端点：telegram 桩回 1x1 PNG（成功路径→blob 头像）、
        # whatsapp 桩回 404（无头像→img 自摘字母底留守）——受控加载两条路都验
        import base64
        _png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
        page.route("**/api/platforms/telegram/*/avatar*", lambda r: r.fulfill(
            status=200, content_type="image/png", body=_png))
        page.route("**/api/platforms/whatsapp/*/avatar*",
                   lambda r: r.fulfill(status=404))

        try:
            page.goto(base + "/workspace/cockpit", wait_until="domcontentloaded")
            page.wait_for_function("() => !!window.CK", timeout=20000)
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP] 驾驶舱页未就绪（{str(e)[:80]}）")
            browser.close()
            return 0
        # 首访清 hint 记忆（保证「首次引导」形态可测）后重载一次
        page.evaluate("() => { try{ localStorage.removeItem('aitr.ck.hint.v1'); }catch(_e){} }")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.CK", timeout=20000)
        page.wait_for_timeout(900)

        st = page.evaluate(_STATE_JS)

        print("== 1. KPI 单口径（新鲜/积压分账，接管超时豁免）==")
        ck.check("需介入=4（5 项中 1 项被折进积压）", st["kQueue"] == "4",
                 f"got {st['kQueue']}")
        ck.check("历史积压=1", st["kStale"] == "1", f"got {st['kStale']}")
        ck.check("接管中=1", st["kTakeover"] == "1")
        ck.check("最久等待取新鲜区最大（≈25h 接管超时，人话格式「1 天 1 小时」）",
                 st["kOldest"].startswith("1 天"), f"got {st['kOldest']}")
        ck.check("主列表 4 张卡", len(st["mainCards"]) == 4)
        ck.check(">72h 接管超时留在主列表（豁免折叠）",
                 any("接管超时" in " ".join(c["kinds"]) for c in st["mainCards"]))

        print("== 2. 首次引导条 ==")
        ck.check("首次访问引导条可见", st["hintVisible"])
        page.evaluate("() => window.CK.dismissHint()")
        ck.check("「知道了」后隐藏",
                 not page.evaluate("() => document.getElementById('ck-hint').offsetParent !== null"))
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.CK", timeout=20000)
        page.wait_for_timeout(900)
        st = page.evaluate(_STATE_JS)
        ck.check("刷新后仍隐藏（localStorage 记忆）", not st["hintVisible"])

        print("== 3. 分类筛选 chips ==")
        ck.check("chips 渲染（全部 + ≥3 类）", len(st["chips"]) >= 4,
                 f"got {len(st['chips'])}")
        page.evaluate("() => window.CK.setFilter('waiting')")
        page.wait_for_timeout(200)
        st = page.evaluate(_STATE_JS)
        ck.check("过滤后只剩「客户在等」卡",
                 len(st["mainCards"]) == 1
                 and any("客户在等" in " ".join(c["kinds"]) for c in st["mainCards"]))
        page.evaluate("() => window.CK.setFilter('')")
        page.wait_for_timeout(200)
        st = page.evaluate(_STATE_JS)
        ck.check("回「全部」恢复 4 张", len(st["mainCards"]) == 4)

        print("== 4. 历史积压折叠区 ==")
        ck.check("折叠头可见且带计数", st["staleHeaderVisible"]
                 and "1" in st["staleHeaderText"], st["staleHeaderText"])
        ck.check("默认收起（积压卡不可见）", not st["staleListVisible"])
        page.evaluate("() => window.CK.toggleStale()")
        page.wait_for_timeout(200)
        st = page.evaluate(_STATE_JS)
        ck.check("点开后渲染积压卡（陈年客户）",
                 st["staleListVisible"] and len(st["staleCards"]) == 1
                 and any("陈年客户" in c["nm"] for c in st["staleCards"]))

        print("== 5. 「已处理」按钮 + resolve 契约 ==")
        has_resolve = any(any("已处理" in b for b in c["btns"]) for c in st["mainCards"])
        no_resolve_on_waiting = all(
            not any("已处理" in b for b in c["btns"])
            for c in st["mainCards"] if "客户在等" in " ".join(c["kinds"]))
        ck.check("needs_human 卡渲染「已处理」", has_resolve)
        ck.check("非 needs_human 卡不渲染", no_resolve_on_waiting)
        # 确认桩放行 → 点 nh1 的已处理 → 浏览器层拦截 POST（零生产写入）；
        # 确认桩同时放行随后的「送进学习队列」二次确认 → feed POST 也应到拦截层
        page.evaluate("() => { window.__wsConfirm = () => Promise.resolve(true); }")
        page.evaluate("() => window.CK.resolve('telegram:a:nh1')")
        page.wait_for_timeout(800)
        ck.check("resolve POST 被拦截层收到且 cid 正确",
                 len(resolve_posts) == 1
                 and resolve_posts[0].get("conversation_id") == "telegram:a:nh1",
                 json.dumps(resolve_posts, ensure_ascii=False))
        ck.check("处理后喂料：feed POST 带客户原话",
                 len(feed_posts) == 1
                 and feed_posts[0].get("query") == "我要退款，等了很久了",
                 json.dumps(feed_posts, ensure_ascii=False))
        beacons = page.evaluate("() => window.__beacons.length")
        ck.check("遥测桩收到 beacon（生产零灌水）", beacons >= 2, f"beacons={beacons}")

        print("== 6. 特性探测降级（旧后端无 caps）==")
        state["mode"] = "nocaps"
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.CK", timeout=20000)
        page.wait_for_timeout(900)
        st = page.evaluate(_STATE_JS)
        all_cards = st["mainCards"] + st["staleCards"]
        ck.check("全页零「已处理」按钮",
                 all(not any("已处理" in b for b in c["btns"]) for c in all_cards))
        ck.check("其余功能不受影响（4 张卡照常）", len(st["mainCards"]) == 4)

        print("== 7. 空态 + ROI 行 ==")
        state["mode"] = "empty"
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.CK", timeout=20000)
        page.wait_for_timeout(1200)
        st = page.evaluate(_STATE_JS)
        ck.check("空态文案渲染", bool(st["emptyBig"]), st["emptyBig"])
        ck.check("ROI 行出「57」", "57" in st["roiText"], st["roiText"])
        ck.check("空态学习队列引流行（4 条待审 + 链接）",
                 "4" in st["learnText"] and "审核" in st["learnText"], st["learnText"])
        ck.check("空态无源警示（全 ok）", not st["srcWarnVisible"])

        print("== 8/9/10. 源降级警示 / 可读名兜底 / 接管联动 ==")
        state["mode"] = "full"
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.CK", timeout=20000)
        page.wait_for_timeout(900)
        st = page.evaluate(_STATE_JS)
        ck.check("waiting 源 error → 警示行可见", st["srcWarnVisible"])
        ck.check("裸 id 不上屏（兜底成「客户（尾号 xxxx）」）",
                 not st["bodyHasRawCid"]
                 and any(c["nm"].startswith("客户（尾号") for c in st["mainCards"]))
        ck.check("有 username 的裸 id 显示 @username",
                 any(c["nm"] == "@hao123" for c in st["mainCards"]))
        tko_card = next((c for c in st["mainCards"]
                         if "接管超时" in " ".join(c["kinds"])), None)
        ck.check("接管在场的卡按钮=「交还 AI」",
                 bool(tko_card) and any("交还" in b for b in tko_card["btns"]))
        ck.check("右栏在场行渲染（agentA）", "agentA" in st["railTk"])

        print("== 11. 身份卡（P3：头像 / 原话引用 / 平台名 / 接管人归行）==")
        nh_card = next((c for c in st["mainCards"] if c["nm"] == "客户甲"), None)
        draft_card = next((c for c in st["mainCards"]
                           if "草稿待审" in " ".join(c["kinds"])), None)
        ck.check("全部主卡渲染头像", all(c["hasAva"] for c in st["mainCards"]))
        ck.check("needs_human 卡引用客户原话",
                 bool(nh_card) and "客户说" in nh_card["quote"]
                 and "我要退款" in nh_card["quote"], (nh_card or {}).get("quote", ""))
        ck.check("接管超时卡引用 last_text（detail=接管人不冒充原话）",
                 bool(tko_card) and "麻烦尽快发货" in tko_card["quote"]
                 and "agentA" not in tko_card["quote"],
                 (tko_card or {}).get("quote", ""))
        ck.check("接管人并进 why 行",
                 bool(tko_card) and "接管人" in tko_card["why"]
                 and "agentA" in tko_card["why"], (tko_card or {}).get("why", ""))
        ck.check("草稿卡前缀「AI 已拟好草稿」",
                 bool(draft_card) and "AI 已拟好草稿" in draft_card["quote"]
                 and "草稿摘要" in draft_card["quote"],
                 (draft_card or {}).get("quote", ""))
        ck.check("平台显示品牌名（Telegram/WhatsApp/LINE）",
                 bool(nh_card) and nh_card["plat"] == "Telegram"
                 and any(c["plat"] == "WhatsApp" for c in st["mainCards"]))
        wa_q = next((c for c in st["mainCards"] if c["plat"] == "WhatsApp"), None)
        ck.check("纯媒体末条 → 引用渲染「[图片]」占位（photo→image 归一）",
                 bool(wa_q) and "[图片]" in wa_q["quote"]
                 and "客户说" in wa_q["quote"], (wa_q or {}).get("quote", ""))

        print("== 11b. 头像代理懒加载（P4：telegram 桩回 PNG / whatsapp 桩回 404）==")
        # 代理 fetch 是渲染后异步受控加载 —— 多等一拍再取快照
        page.wait_for_timeout(900)
        st = page.evaluate(_STATE_JS)
        tko_card = next((c for c in st["mainCards"]
                         if "接管超时" in " ".join(c["kinds"])), None)
        wa_card = next((c for c in st["mainCards"]
                        if c["plat"] == "WhatsApp"), None)
        nh_card = next((c for c in st["mainCards"] if c["nm"] == "客户甲"), None)
        ck.check("无直链的 telegram 卡经代理加载出 blob 头像",
                 bool(tko_card) and tko_card["avaSrc"].startswith("blob:"),
                 (tko_card or {}).get("avaSrc", ""))
        ck.check("代理 404（whatsapp）→ img 自摘字母底留守",
                 bool(wa_card) and not wa_card["avaSrc"] and wa_card["hasAva"],
                 (wa_card or {}).get("avaSrc", ""))
        ck.check("坏直链（404 png）→ onerror 自摘不裂图",
                 bool(nh_card) and not nh_card["avaSrc"] and nh_card["hasAva"],
                 (nh_card or {}).get("avaSrc", ""))

        print("== 12. 账号健康折叠（状态未知收进一行）==")
        # _FLEET：active=直列 ok、banned=直列 bad、connecting=未知 → 折叠行
        ck.check("汇总在（在线 1 / 异常 1）", "1" in st["acctSum"], st["acctSum"])
        ck.check("未知状态折叠行可见且带计数",
                 "1" in st["acctMore"], st["acctMore"])
        ck.check("折叠区默认收起", not st["acctUnVisible"])
        page.evaluate("() => window.CK.toggleAcctUnknown()")
        page.wait_for_timeout(200)
        st = page.evaluate(_STATE_JS)
        ck.check("点开后未知行可见（3 行齐）",
                 st["acctUnVisible"] and st["acctRows"] == 3,
                 f"rows={st['acctRows']}")

        browser.close()
    return ck.summary()


def main() -> int:
    # Windows GBK 控制台打不出「✓」等字符（空态文案里就有）——门禁输出统一 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="驾驶舱页真浏览器门禁（route-mock 零副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[SKIP] playwright 未安装（pip install playwright && playwright install chromium）")
        return 0

    token = read_token(args.data_root)
    if not token:
        print("[ABORT] 读不到 web_admin.auth_token（--data-root 指错？）")
        return 2

    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达 {args.base}（{e}）")
        return 0

    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
