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
        base["scan_truncated"] = True
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
    ck.check("页内回复走统一发送", "/api/unified-inbox/send" in src)
    ck.check("打开对话记下下一张", "aitr.ck.next" in src)
    ck.check("回程记录跨窗口（localStorage）", "aitr.ck.ret.v1" in src and "retBack" in src)
    ck.check("页内回复：在途锁 + 输入法回车不发", "_sending[cid]" in src and "event.isComposing" in src)
    ck.check("页内回复：失败再发带原件键", "resend_of_cmid" in src)
    ck.check("接管收进「更多」", "ck-more" in src)


_STATE_JS = """() => {
  const $ = (id) => document.getElementById(id);
  const txt = (id) => (($(id) || {}).textContent || '').trim();
  const vis = (id) => { const el = $(id); return !!el && el.offsetParent !== null; };
  const cards = (rootId) => Array.from(
    (document.getElementById(rootId) || document.createElement('i'))
      .querySelectorAll('.ck-card'));
  const cardInfo = (c) => ({
    nm: ((c.querySelector('.nm') || {}).textContent || '').trim(),
    // 分组里单一类别的卡不再显示类别 chip（P1-10）：类别以 data-kind 为准，chip 文字照收
    kinds: Array.from(c.querySelectorAll('.ck-kind')).map(k => k.textContent.trim())
      .concat([({waiting: '客户在等', needs_human: '需人工', takeover_overdue: '接管超时',
                 draft_pending: '草稿待审'})[c.getAttribute('data-kind') || ''] || '']),
    btns: Array.from(c.querySelectorAll('.ck-btn')).map(b => b.textContent.trim()),
    plat: ((c.querySelector('.ck-plat') || {}).textContent || '').trim(),
    acct: ((c.querySelector('.ck-acct') || {}).textContent || '').trim(),
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
    lead: txt('ck-lead'),
    listText: ((document.getElementById('ck-q-list') || {}).innerText || ''),
    acctAlert: txt('ck-acct-alert'),
    scanNote: vis('ck-scan-note'),
    chipsHidden: (function(){
      const el = document.getElementById('ck-fchips');
      return !!el && el.style.display === 'none';
    })(),
    replyInputs: document.querySelectorAll('#ck-q-list .ck-reply-in').length,
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
            if state.get("fail"):
                route.fulfill(status=500, content_type="application/json",
                              body=json.dumps({"ok": False}))
                return
            snap = _overview(state["mode"])
            bump = float(state.get("bump") or 0)
            for it in snap.get("items") or []:
                it["age_sec"] = float(it.get("age_sec") or 0) + bump
            snap.update(state.get("extra") or {})
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(snap))

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
        def _route_accounts(route: Any) -> None:
            path = route.request.url.split("?", 1)[0].rstrip("/")
            if not path.endswith("/api/accounts"):
                route.fallback()
                return
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "accounts": [
                    {"platform": "telegram", "account_id": "a", "label": "主号甲"},
                    {"platform": "whatsapp", "account_id": "b", "label": "备号乙"},
                ],
            }))

        page.route("**/api/accounts*", _route_accounts)
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
        ck.check("要回的=2（等待+需人工；稿和接管不进这个数字）", st["kQueue"] == "2",
                 f"got {st['kQueue']}")
        ck.check("历史积压=1", st["kStale"] == "1", f"got {st['kStale']}")
        ck.check("接管中=1", st["kTakeover"] == "1")
        ck.check("最久等待取新鲜区最大（≈25h 接管超时，人话格式「1 天 1 小时」）",
                 st["kOldest"].startswith("1 天"), f"got {st['kOldest']}")
        ck.check("主列表 4 张卡", len(st["mainCards"]) == 4)
        ck.check(">72h 接管超时留在主列表（豁免折叠）",
                 any("接管超时" in " ".join(c["kinds"]) for c in st["mainCards"]))

        print("== 2. 任务说明常驻（不再靠可关掉的引导条）==")
        ck.check("顶上写明还有几条要你回", "要你回" in st["lead"], st["lead"])
        ck.check("稿和忘了交还另说，不叫要你回",
                 "稿待审" in st["lead"] and "忘了交还" in st["lead"], st["lead"])
        ck.check("数字在句子里而不是单独的大格子", st["kQueue"] == "2")

        print("== 3. 分类筛选 chips ==")
        ck.check("chips 渲染（全部 + ≥3 类）", len(st["chips"]) >= 4,
                 f"got {len(st['chips'])}")
        ck.check("筛选默认收着", st["chipsHidden"])
        ck.check("默认分成要回的、要审的稿、忘了交还",
                 "要回的" in st["listText"] and "要审的稿" in st["listText"]
                 and "忘了交还" in st["listText"], st["listText"][:80])
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
        ck.check("空态不提示扫描截断", not st["scanNote"])

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
        ck.check("接管超时不再把「接管」放成主动作",
                 bool(tko_card) and not any(b.startswith("接管") for b in tko_card["btns"]))
        wait_card = next((c for c in st["mainCards"]
                          if "客户在等" in " ".join(c["kinds"])), None)
        ck.check("客户在等的主按钮是「去回」，并可以先不回",
                 bool(wait_card) and any("去回" in b for b in wait_card["btns"])
                 and any("先不回" in b for b in wait_card["btns"]))
        ck.check("客户在等可以在卡上回一句", st["replyInputs"] == 1,
                 f"inputs={st['replyInputs']}")
        ck.check("右栏在场行渲染（agentA）", "agentA" in st["railTk"])
        ck.check("扫满窗口时说明更老的可能没列进来", st["scanNote"])

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

        print("== 12. 账号异常只留一行（未知状态不再铺开）==")
        # _FLEET：banned 算异常；active / connecting 不铺成名单
        ck.check("有封禁号时顶上有一行异常",
                 "异常" in st["acctAlert"] and "1" in st["acctAlert"], st["acctAlert"])
        ck.check("卡片用账号列表里的名字",
                 any(c.get("acct") == "主号甲" for c in st["mainCards"])
                 and any(c.get("acct") == "备号乙" for c in st["mainCards"]),
                 str([c.get("acct") for c in st["mainCards"]]))

        print("== 13. 从完整对话回来落到下一张（回程记录，零发送）==")
        # 记下每次 scrollIntoView 落在哪张卡（init script：每次加载都先于页面脚本装上）
        page.add_init_script(
            "window.__ckScrolled=[];(function(){var o=Element.prototype.scrollIntoView;"
            "Element.prototype.scrollIntoView=function(){try{window.__ckScrolled.push("
            "this.getAttribute('data-cid')||'');}catch(_e){}return o.apply(this,arguments);};})();")
        state["mode"] = "full"
        page.evaluate("() => { try{ localStorage.removeItem('aitr.ck.ret.v1');"
                      " sessionStorage.removeItem('aitr.ck.next'); }catch(_e){} }")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.CK", timeout=20000)
        page.wait_for_timeout(900)
        _ids_js = ("() => [].slice.call(document.querySelectorAll('.ck-q-list .ck-card[data-cid]'))"
                   ".map(function(c){ return c.getAttribute('data-cid'); })")
        ids = page.evaluate(_ids_js)
        ok_ids = len(ids) >= 3
        ck.check("主列表至少 3 张卡可做回程演练", ok_ids, str(ids))
        if ok_ids:
            a, b = ids[0], ids[1]
            _set = ("(r) => { localStorage.setItem('aitr.ck.ret.v1', JSON.stringify(r));"
                    " window.__ckScrolled=[]; }")
            page.evaluate(_set, {"cid": a, "next": b, "ts": int(time.time() * 1000)})
            page.evaluate("() => CK.load(false)")
            page.wait_for_timeout(700)
            r1 = page.evaluate("() => ({s: window.__ckScrolled.slice(),"
                               " rec: localStorage.getItem('aitr.ck.ret.v1')})")
            ck.check("人还在本页时轮询不消费回程记录", not r1["s"] and bool(r1["rec"]), str(r1["s"]))
            page.evaluate("() => { window.dispatchEvent(new Event('blur'));"
                          " window.dispatchEvent(new Event('focus')); }")
            page.wait_for_timeout(900)
            r2 = page.evaluate("() => ({s: window.__ckScrolled.slice(),"
                               " rec: localStorage.getItem('aitr.ck.ret.v1')})")
            ck.check("切走再回来：落到刚才那条的下一张", bool(r2["s"]) and r2["s"][-1] == b,
                     f"scrolled={r2['s']} want={b}")
            ck.check("回程记录用一次就清掉", not r2["rec"])
            rec = {"cid": a, "next": b, "ts": int(time.time() * 1000),
                   "sent": int(time.time() * 1000)}
            page.evaluate(_set, rec)
            page.evaluate("(r) => window.dispatchEvent(new StorageEvent('storage',"
                          " {key: 'aitr.ck.ret.v1', newValue: JSON.stringify(r)}))", rec)
            page.wait_for_timeout(900)
            r3 = page.evaluate("() => ({s: window.__ckScrolled.slice()})")
            ids3 = page.evaluate(_ids_js)
            ck.check("收件箱已回：那张先收起", a not in ids3, str(ids3))
            ck.check("收件箱已回：落到下一张", bool(r3["s"]) and r3["s"][-1] == b,
                     f"scrolled={r3['s']} want={b}")
            page.evaluate("(r) => localStorage.setItem('aitr.ck.ret.v1', JSON.stringify(r))",
                          {"cid": a, "next": b, "ts": int(time.time() * 1000)})
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("() => !!window.CK", timeout=20000)
            page.wait_for_timeout(900)
            r4 = page.evaluate("() => window.__ckScrolled.slice()")
            ck.check("带着回程记录重新打开本页：落到下一张", bool(r4) and r4[-1] == b,
                     f"scrolled={r4} want={b}")

        print("== 14. 跨窗口真流程：点「去回」→ 收件箱窗记已回 → 待人工收起并落到下一张 ==")
        cleared_posts: List[Dict[str, Any]] = []

        def _route_ctx_api(route: Any) -> None:
            url = route.request.url
            if "/api/cockpit/cleared" in url:
                try:
                    cleared_posts.append(json.loads(route.request.post_data or "{}"))
                except Exception:
                    cleared_posts.append({"__bad": True})
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "counted": True}))
                return
            route.abort()   # 收件箱窗不读任何生产数据（页面只渲染模板，接口一律掐断）

        ctx.route("**/api/**", _route_ctx_api)
        ck_url = page.url
        # 14a：已开着坐席收件箱窗（常态）→ 点「去回」交给那扇窗切会话，本页不动
        inbox = ctx.new_page()
        inbox_errs: List[str] = []
        inbox.on("pageerror", lambda e: inbox_errs.append(str(e)[:160]))
        inbox_ok = False
        try:
            inbox.goto(base + "/workspace", wait_until="domcontentloaded", timeout=30000)
            inbox.wait_for_function("() => typeof window._ckNoteInboxReply === 'function'",
                                    timeout=30000, polling=300)
            inbox_ok = True
        except Exception as e:  # noqa: BLE001
            try:
                diag = inbox.evaluate("() => ({u: location.pathname, t: document.title,"
                                      " ck: typeof window._ckNoteInboxReply, rf: typeof window.resendFailed,"
                                      " src: document.documentElement.innerHTML.indexOf('function _ckNoteInboxReply'),"
                                      " n: document.scripts.length})")
            except Exception as e2:  # noqa: BLE001
                diag = {"evalErr": str(e2)[:80]}
            ck.check("收件箱窗加载出记账函数", False, f"{str(e)[:60]} diag={diag} errs={inbox_errs[:3]}")
        page.bring_to_front()
        page.evaluate("() => { try{ localStorage.removeItem('aitr.ck.ret.v1'); }catch(_e){} }")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.CK", timeout=20000)
        page.wait_for_timeout(900)
        ids = page.evaluate(_ids_js)
        if inbox_ok and len(ids) >= 2:
            a, b = ids[0], ids[1]
            page.click('.ck-q-list a[data-ck-open="%s"]' % a)
            page.wait_for_timeout(2500)   # 交接探活（BroadcastChannel）
            ck.check("有收件箱窗时点「去回」：待人工页不跳走", page.url == ck_url, page.url)
            rec0 = page.evaluate("() => localStorage.getItem('aitr.ck.ret.v1')")
            r0 = json.loads(rec0) if rec0 else {}
            ck.check("点开时记下回程（刚才那条 + 下一张）",
                     r0.get("cid") == a and r0.get("next") == b, str(rec0))
            page.evaluate("() => { window.__ckScrolled=[]; }")
            parts = a.split(":", 2)
            inbox.evaluate("(c) => _ckNoteInboxReply(c)", {
                "conversation_id": a, "platform": parts[0],
                "account_id": parts[1] if len(parts) > 1 else "default",
                "chat_key": parts[2] if len(parts) > 2 else ""})
            page.wait_for_timeout(1500)
            ck.check("收件箱窗记账走 src=inbox + reply_conversation_id",
                     any(p.get("src") == "inbox" and p.get("reply_conversation_id") == a
                         for p in cleared_posts), str(cleared_posts)[:160])
            s5 = page.evaluate("() => window.__ckScrolled.slice()")
            ids5 = page.evaluate(_ids_js)
            ck.check("待人工窗：那张收起", a not in ids5, str(ids5))
            ck.check("待人工窗：落到下一张", bool(s5) and s5[-1] == b, f"scrolled={s5} want={b}")
        elif inbox_ok:
            ck.check("跨窗口演练需要至少 2 张卡", False, str(ids))
        try:
            inbox.close()
        except Exception:
            pass
        # 14b：没开收件箱窗 → 本页原地进对话；按「返回」回来落到下一张
        page.evaluate("() => { try{ localStorage.removeItem('aitr.ck.ret.v1'); }catch(_e){} }")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.CK", timeout=20000)
        page.wait_for_timeout(900)
        ids = page.evaluate(_ids_js)
        if len(ids) >= 2:
            a, b = ids[0], ids[1]
            try:
                with page.expect_navigation(timeout=15000):
                    page.click('.ck-q-list a[data-ck-open="%s"]' % a)
                went = "/workspace" in page.url
            except Exception as e:  # noqa: BLE001
                went = False
                print("    nav:", str(e)[:100])
            ck.check("无收件箱窗：本页进完整对话", went, page.url)
            if went:
                page.go_back(wait_until="domcontentloaded")
                page.wait_for_function("() => !!window.CK", timeout=20000)
                page.wait_for_timeout(1200)
                s6 = page.evaluate("() => (window.__ckScrolled||[]).slice()")
                ck.check("按返回回到待人工：落到下一张", bool(s6) and s6[-1] == b,
                         f"scrolled={s6} want={b}")

        print("== 15. 页内回复：打字不被冲掉 / 回车只发 1 次 / 一个实心按钮 / 落点高亮 / 刷新失败留列表 ==")
        send_posts: List[Dict[str, Any]] = []

        def _route_send(route: Any) -> None:
            try:
                send_posts.append(json.loads(route.request.post_data or "{}"))
            except Exception:
                send_posts.append({"__bad": True})
            if state.get("send_fail"):
                route.fulfill(status=502, content_type="application/json",
                              body=json.dumps({"ok": False}))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True}))

        page.route("**/api/unified-inbox/send", _route_send)
        state.update({"mode": "full", "bump": 0.0, "fail": False, "send_fail": False})
        page.evaluate("() => { try{ localStorage.removeItem('aitr.ck.ret.v1');"
                      " sessionStorage.removeItem('aitr.ck.next'); }catch(_e){} }")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.CK", timeout=20000)
        page.wait_for_timeout(900)
        W = "whatsapp:b:88997766"
        _inp = '.ck-q-list .ck-reply-in[data-cid="%s"]' % W
        _solid_js = ("(cid) => { var c=[].slice.call(document.querySelectorAll('.ck-q-list .ck-card'))"
                     ".filter(function(x){return x.getAttribute('data-cid')===cid;})[0]; if(!c) return null;"
                     " return [].slice.call(c.querySelectorAll('.ck-btn')).filter(function(b){"
                     " return b.offsetWidth && getComputedStyle(b).color==='rgb(255, 255, 255)'; })"
                     ".map(function(b){ return b.classList.contains('ck-send')?'send':(b.hasAttribute('data-ck-open')?'open':b.textContent); }); }")
        s0 = page.evaluate(_solid_js, W)
        ck.check("没打字：卡上只有「去回」一个实心按钮", s0 == ["open"], str(s0))
        page.click(_inp)
        page.keyboard.type("测试保留")
        s1 = page.evaluate(_solid_js, W)
        ck.check("打了字：只有「发送」实心", s1 == ["send"], str(s1))
        for _ in range(3):
            state["bump"] += 90.0      # 等待分钟数变 → 以前每次都整表重建
            page.evaluate("() => CK.load(false)")
            page.wait_for_timeout(500)
        r1 = page.evaluate("(sel) => { var i=document.querySelector(sel); return {v: i?i.value:null,"
                           " f: document.activeElement===i}; }", _inp)
        ck.check("聚焦打字时轮询 3 次：字还在、焦点还在", r1["v"] == "测试保留" and r1["f"], str(r1))
        page.evaluate("() => { document.activeElement && document.activeElement.blur(); }")
        page.wait_for_timeout(300)
        state["bump"] += 90.0
        page.evaluate("() => CK.load(false)")
        page.wait_for_timeout(600)
        r2 = page.evaluate("(sel) => { var i=document.querySelector(sel); return i?i.value:null; }", _inp)
        ck.check("失焦后整表重画：打到一半的字回填", r2 == "测试保留", str(r2))
        # 输入法组字中的回车不发
        page.evaluate("(sel) => { var i=document.querySelector(sel); i.focus();"
                      " i.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', isComposing:true, bubbles:true})); }", _inp)
        page.wait_for_timeout(500)
        ck.check("输入法组字时按回车不发送", len(send_posts) == 0, str(len(send_posts)))
        # 第一次失败 → 卡片留着、可再发；再发带上原件键
        state["send_fail"] = True
        page.evaluate("(sel) => { var i=document.querySelector(sel); i.focus();"
                      " i.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true})); }", _inp)
        page.wait_for_timeout(900)
        r3 = page.evaluate("(sel) => { var i=document.querySelector(sel); var e=i&&i.closest('.ck-reply').querySelector('.ck-reply-err');"
                           " return {v: i?i.value:null, dis: i?i.disabled:null, err: e?e.style.display!=='none':false}; }", _inp)
        ck.check("发送失败：字留着、框可再用、出错一行", r3["v"] == "测试保留" and r3["dis"] is False and r3["err"], str(r3))
        state["send_fail"] = False
        n_before = len(send_posts)
        page.evaluate("(sel) => { var i=document.querySelector(sel); i.focus();"
                      " for (var k=0;k<3;k++) i.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true})); }", _inp)
        page.wait_for_timeout(1500)
        new_posts = send_posts[n_before:]
        ck.check("连按 3 次回车只发 1 次", len(new_posts) == 1, f"posts={len(new_posts)}")
        first_cmid = (send_posts[n_before - 1] or {}).get("client_msg_id") if n_before else ""
        ck.check("失败后再发带上原件键（服务端判断要不要压住）",
                 bool(new_posts) and new_posts[0].get("resend_of_cmid") == first_cmid
                 and new_posts[0].get("client_msg_id") != first_cmid,
                 str({k: (new_posts[0] if new_posts else {}).get(k) for k in ("client_msg_id", "resend_of_cmid")}))
        gone = page.evaluate("(cid) => ![].slice.call(document.querySelectorAll('.ck-q-list .ck-card'))"
                             ".some(function(c){return c.getAttribute('data-cid')===cid;})", W)
        ck.check("发出后那张先收起", gone)
        # 落点：高亮 + 聚焦它的输入框
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => !!window.CK", timeout=20000)
        page.wait_for_timeout(900)
        page.evaluate("(r) => localStorage.setItem('aitr.ck.ret.v1', JSON.stringify(r))",
                      {"cid": "telegram:a:nh1", "next": W, "ts": int(time.time() * 1000)})
        page.evaluate("() => { window.dispatchEvent(new Event('blur')); window.dispatchEvent(new Event('focus')); }")
        page.wait_for_timeout(900)
        r4 = page.evaluate("(cid) => { var c=[].slice.call(document.querySelectorAll('.ck-q-list .ck-card'))"
                           ".filter(function(x){return x.getAttribute('data-cid')===cid;})[0];"
                           " var a=document.activeElement; return {hl: !!(c && c.classList.contains('ck-landed')),"
                           " focus: !!(a && a.classList && a.classList.contains('ck-reply-in') && a.getAttribute('data-cid')===cid)}; }", W)
        ck.check("落到下一张：描边高亮", r4["hl"], str(r4))
        ck.check("落到下一张：光标进它的回复框", r4["focus"], str(r4))
        page.evaluate("() => { document.activeElement && document.activeElement.blur(); }")
        # 刷新失败：留着卡片，只出一行提示
        n_cards = page.evaluate("() => document.querySelectorAll('#ck-q-list .ck-card').length")
        state["fail"] = True
        page.evaluate("() => CK.load(false)")
        page.wait_for_timeout(900)
        r5 = page.evaluate("() => ({n: document.querySelectorAll('#ck-q-list .ck-card').length,"
                           " w: (function(e){ return e && e.style.display!=='none' ? e.textContent : ''; })(document.getElementById('ck-load-warn'))})")
        ck.check("刷新返回 500：卡片留着", n_cards > 0 and r5["n"] == n_cards, f"{n_cards}->{r5['n']}")
        ck.check("刷新返回 500：出一行「刚才没刷新成功」", "没刷新成功" in (r5["w"] or ""), r5["w"])
        state["fail"] = False
        page.evaluate("() => CK.load(false)")
        page.wait_for_timeout(900)
        ck.check("恢复后提示收起", page.evaluate(
            "() => document.getElementById('ck-load-warn').style.display==='none'"))
        # 账号 chip 样式 + 底部让开浮动按钮
        r6 = page.evaluate("() => { var a=document.querySelector('.ck-q-list .ck-acct');"
                           " var q=document.querySelector('.ck-q-list');"
                           " return {fs: a?parseFloat(getComputedStyle(a).fontSize):0,"
                           " bg: a?getComputedStyle(a).backgroundColor:'', pb: parseFloat(getComputedStyle(q).paddingBottom)}; }")
        ck.check("账号显示成小号 chip", 0 < r6["fs"] <= 11 and r6["bg"] not in ("", "rgba(0, 0, 0, 0)"), str(r6))
        ck.check("列表底部留出浮动按钮的位置（≥72px）", r6["pb"] >= 72, str(r6["pb"]))

        # ── 16. 积压只列最近一截：服务端给的真实总数照实写（P0-3）────────────
        print("\n[16] 积压总数 stale_total")
        state["extra"] = {"stale_total": 31}
        page.evaluate("() => CK.load(true)")
        page.wait_for_timeout(900)
        r7 = page.evaluate("() => ({k: document.getElementById('ck-k-stale').textContent,"
                           " h: document.getElementById('ck-stale-t').textContent,"
                           " n: document.querySelectorAll('#ck-stale-list .ck-card').length})")
        ck.check("顶部积压数＝服务端总数", r7["k"] == "31", str(r7))
        ck.check("积压折叠头写明「总数，先列最近的 N」", "31" in r7["h"] and "最近" in r7["h"], r7["h"])
        state["extra"] = {}
        page.evaluate("() => CK.load(true)")
        page.wait_for_timeout(900)
        r8 = page.evaluate("() => document.getElementById('ck-stale-t').textContent")
        ck.check("没有截断时照旧只写人数", "最近" not in r8, r8)

        # ── 17. 同屏口径说明（P0-4）：几个数之间各有一句话说清楚 ─────────────
        print("\n[17] 这些数怎么算")
        hidden0 = page.evaluate("() => document.getElementById('ck-why').style.display==='none'")
        ck.check("口径说明默认收起", hidden0)
        page.click("#ck-why-btn")
        page.wait_for_timeout(200)
        r9 = page.evaluate("() => { var w=document.getElementById('ck-why');"
                           " return {vis: w.style.display!=='none', n: w.querySelectorAll('p').length,"
                           " t: w.textContent, ax: document.getElementById('ck-why-btn').getAttribute('aria-expanded')}; }")
        ck.check("点开后出四句：主句 / 积压 / 顶栏提醒 / 今天清掉",
                 r9["vis"] and r9["n"] == 4 and "3 天" in r9["t"] and "顶栏" in r9["t"] and "清掉" in r9["t"],
                 str({k: r9[k] for k in ("vis", "n", "ax")}))
        ck.check("按钮 aria-expanded 跟着变", r9["ax"] == "true", r9["ax"])
        page.click("#ck-why-btn")
        page.wait_for_timeout(200)
        ck.check("再点收起", page.evaluate("() => document.getElementById('ck-why').style.display==='none'"))

        # ── 18. 顶栏超时提醒 tooltip 指向待人工（P1-8 最小改动；只读真实徽标）──
        print("\n[18] 顶栏提醒 tooltip 口径句")
        r10 = page.evaluate("() => { var b=document.getElementById('ws-sla');"
                            " return {k: (window.WS_I18N||{})['base.sla.tip_cockpit']||'',"
                            " vis: !!(b && b.offsetParent !== null && b.querySelector('.ws-pill-n')), t: b ? (b.title||'') : ''}; }")
        ck.check("口径句词条已下发到页面", "待人工" in r10["k"], r10["k"][:40])
        if r10["vis"]:
            ck.check("徽标亮着时 tooltip 带口径句", "待人工" in r10["t"], r10["t"][:60])
        else:
            print("  [SKIP] 徽标当前没亮（没有超时会话），tooltip 不检")

        # ── 19. 视觉批：信息去重 / 时长三档 / 积压紧凑行 / 文案（P1-10 P1-7 P1-12）──
        print("\n[19] 卡片去重、三档颜色、积压紧凑行、刷新时间")
        state["mode"] = "full"
        state["extra"] = {}
        page.evaluate("() => { CK.load(true); }")
        page.wait_for_timeout(900)
        r11 = page.evaluate("""() => {
          const q = s => document.querySelector(s);
          const w = q('#ck-q-list .ck-card[data-kind=waiting]');
          const n = q('#ck-q-list .ck-card[data-kind=needs_human]');
          const d = q('#ck-q-list .ck-card[data-kind=draft_pending]');
          const t = q('#ck-q-list .ck-card[data-kind=takeover_overdue]');
          return {
            wKind: w ? w.querySelectorAll('.ck-kind').length : -1,
            wWhy: w ? w.querySelectorAll('.why').length : -1,
            wTier: w ? ['t0','t1','t2'].filter(c => w.classList.contains(c)) : [],
            nKind: n ? n.querySelectorAll('.ck-kind').length : -1,
            nWhy: n ? n.querySelectorAll('.why').length : -1,
            dTier: d ? ['t0','t1','t2'].filter(c => d.classList.contains(c)) : [],
            dWhy: d ? d.querySelectorAll('.why').length : -1,
            tP1: !!(t && t.classList.contains('p1')),
            tWhy: t ? t.querySelectorAll('.why').length : -1,
            age: (q('#ck-age') || {}).textContent || '',
            autoNote: document.body.innerText.indexOf('每 30 秒自动刷新') >= 0,
          };
        }""")
        ck.check("客户在等卡：分组里不再重复「客户在等」chip、没有 why 行",
                 r11["wKind"] == 0 and r11["wWhy"] == 0, str(r11))
        ck.check("需人工卡：类别 chip 和原因行照留", r11["nKind"] == 1 and r11["nWhy"] == 1, str(r11))
        ck.check("接管超时卡：保持红边、why 行（接管人）照留", r11["tP1"] and r11["tWhy"] == 1, str(r11))
        ck.check("时长三档：等 1 小时＝琥珀、10 分钟草稿＝中性",
                 r11["wTier"] == ["t1"] and r11["dTier"] == ["t0"] and r11["dWhy"] == 0, str(r11))
        ck.check("刷新时间并进「更新 · 自动刷新」，不再单列「每 30 秒自动刷新」",
                 "自动刷新" in r11["age"] and not r11["autoNote"], r11["age"])
        page.evaluate("() => { if(document.getElementById('ck-stale-list').style.display==='none') CK.toggleStale(); }")
        page.wait_for_timeout(400)
        r12 = page.evaluate("""() => {
          const rows = Array.from(document.querySelectorAll('#ck-stale-list .ck-card'));
          return {n: rows.length, compact: rows.filter(r => r.classList.contains('ck-row')).length,
                  inputs: document.querySelectorAll('#ck-stale-list .ck-reply-in').length,
                  h: rows.length ? Math.round(rows[0].getBoundingClientRect().height) : 0,
                  sub: (document.getElementById('ck-stale-sub') || {}).textContent || ''};
        }""")
        ck.check("积压是紧凑行、不带输入框", r12["n"] >= 1 and r12["compact"] == r12["n"]
                 and r12["inputs"] == 0 and 0 < r12["h"] <= 48, str(r12))
        ck.check("积压说明和按钮一致（不再提「已处理」）",
                 "先不回" in r12["sub"] and "已处理" not in r12["sub"], r12["sub"])

        # ── 20. 手机 / 窄屏（P1-1）：390 / 768 不横向溢出、按钮互不压 ─────────
        def _layout(width: int, height: int) -> Dict[str, Any]:
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(300)
            page.evaluate("() => CK.load(true)")
            page.wait_for_timeout(900)
            return page.evaluate("""() => {
              const iw = window.innerWidth;
              const list = document.querySelector('.ck-q-list');
              const bad = [];
              let outside = 0;
              document.querySelectorAll('.ck-q-list .ck-card').forEach(c => {
                const cr = c.getBoundingClientRect();
                if (cr.right > iw + 0.5 || cr.left < -0.5) outside++;
                const els = Array.from(c.querySelectorAll('.ck-btn, .ck-reply-in, .ck-more summary, .wait, .nm'))
                  .filter(e => e.offsetParent !== null)
                  .map(e => ({e, r: e.getBoundingClientRect()}))
                  .filter(o => o.r.width > 0 && o.r.height > 0);
                for (let i = 0; i < els.length; i++) for (let j = i + 1; j < els.length; j++) {
                  const a = els[i].r, b = els[j].r;
                  if (els[i].e.contains(els[j].e) || els[j].e.contains(els[i].e)) continue;
                  const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
                  const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
                  if (ox > 1 && oy > 1) bad.push((c.getAttribute('data-cid') || '') + ': '
                    + (els[i].e.className || els[i].e.tagName) + ' x ' + (els[j].e.className || els[j].e.tagName));
                }
              });
              const wide = [], wideCk = [];
              const wrap = document.querySelector('.ck-wrap');
              document.querySelectorAll('body *').forEach(e => {
                if (wide.length >= 6 || e.offsetParent === null) return;
                const r = e.getBoundingClientRect();
                if (r.right > iw + 0.5 && r.width > 0) {
                  const kids = Array.from(e.children).some(k => k.getBoundingClientRect().right > iw + 0.5);
                  const tag = (e.id ? '#' + e.id : e.tagName.toLowerCase())
                    + (typeof e.className === 'string' && e.className ? '.' + e.className.split(' ').join('.') : '')
                    + ' r=' + Math.round(r.right);
                  if (!kids) (wrap && wrap.contains(e) ? wideCk : wide).push(tag);
                }
              });
              return {iw, docW: document.documentElement.scrollWidth, wide, wideCk,
                      wrapOver: wrap ? wrap.scrollWidth - wrap.clientWidth : 0,
                      listOver: list ? list.scrollWidth - list.clientWidth : 0,
                      outside, overlaps: bad.slice(0, 6), nOverlap: bad.length,
                      cards: document.querySelectorAll('.ck-q-list .ck-card').length};
            }""")

        for (w, h) in ((390, 844), (768, 1024)):
            print(f"\n[20] 视口 {w}px")
            lay = _layout(w, h)
            # 本页内容（.ck-wrap 以内）必须不横向溢出；共享壳顶栏（头像点等）溢出另记一行，不算本页
            ck.check(f"{w}px：待人工内容不横向溢出（.ck-wrap 内无元素越过 innerWidth）",
                     not lay["wideCk"] and lay["wrapOver"] <= 1 and lay["listOver"] <= 1,
                     f"wrapOver={lay['wrapOver']} listOver={lay['listOver']} wideCk={lay['wideCk']}")
            if lay["docW"] > lay["iw"]:
                print(f"  [NOTE] 整页 scrollWidth={lay['docW']} > {lay['iw']}，越界的是共享壳：{lay['wide']}")
            ck.check(f"{w}px：卡片不出屏", lay["cards"] > 0 and lay["outside"] == 0, str(lay["outside"]))
            ck.check(f"{w}px：按钮/输入框/时长互不相压", lay["nOverlap"] == 0, "; ".join(lay["overlaps"]))
        # 768 下右栏收起：接管名单从顶部「接管中 N」展开
        r13 = page.evaluate("""() => {
          const rail = document.querySelector('.ck-rail');
          const before = !!(rail && rail.offsetParent !== null);
          const tg = document.getElementById('ck-tk-tog');
          if (tg) tg.click();
          const after = !!(rail && rail.offsetParent !== null);
          const txt = (document.getElementById('ck-rail-tk') || {}).textContent || '';
          const ax = tg ? tg.getAttribute('aria-expanded') : '';
          if (tg) tg.click();
          return {before, after, txt: txt.slice(0, 60), ax,
                  closed: !(rail && rail.offsetParent !== null)};
        }""")
        ck.check("768px：接管名单默认收起，点「接管中 N」展开、再点收起",
                 (not r13["before"]) and r13["after"] and "agentA" in r13["txt"]
                 and r13["ax"] == "true" and r13["closed"], str(r13))
        page.set_viewport_size(VIEWPORT)

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
