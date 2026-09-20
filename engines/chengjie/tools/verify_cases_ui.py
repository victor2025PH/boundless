# -*- coding: utf-8 -*-
"""案例跟进页（/cases）真浏览器门禁（Playwright；2026-08-03 案例中心改造收口）。

**为什么需要它**：cases.html 重构后是一页真 JS 应用（筛选/认领/结案模态/编辑保护/
XSS 转义/新旧后端自适配），静态门禁只能证「函数挂了 window、键中英齐备」，证不了
「客户消息里的 HTML 真的不会在运营后台执行」「30s 刷新真的不吞正在输入的备注」。
这些正是当初立项要修的事故面，必须有回归守护，而模板热更新是直上生产的。

与 ``tools/verify_inbox_density.py`` / ``verify_inbox_identity.py`` 同族（同一实例 +
token 登录 + Playwright + SKIP exit 0）。**零生产写入**：案例数据全部经
``page.route`` 注入合成响应；结案模态只验开/关，不点确认；不调用任何写端点。

用法::

    python tools/verify_cases_ui.py              # 门禁模式
    python tools/verify_cases_ui.py --headed     # 肉眼看一遍

覆盖的不变量：
  1. **XSS 逃逸**（本次改造的安全主诉求）：客户消息含 ``<img onerror>`` /
     引号逃逸 payload → 以文本呈现，不产生元素、不执行（window.__xss 不存在）。
  2. 卡片语义：severity 色缘 class、来源徽章、认领人徽章、案号可见。
  3. 认领 UI 新旧后端自适配：行带 ``claimed_by`` 键才渲染认领/释放按钮。
  4. 已结案卡折叠（气泡隐藏）→ 点击展开。
  5. 筛选 chips 计数 + 「已结案」筛选生效；搜索命中案号。
  6. 结案模态：打开 → 快捷标签回填 → 取消关闭（不确认、零写入）。
  7. 编辑保护：备注框有未保存改动时 loadCases 跳过重绘并亮「已暂停」提示。
  8. 空态教育：0 案例 → 立案时机列表（5 条）+ 带「示例」缎带的样例卡。
  9. **身份行**：平台徽标 + 账号短码出现在卡头（坐席不用猜「哪个号的事」）。
  10. **深链 mid**：``csOpenConv`` 拼出的 URL 含 ``&mid=``（工作台直达质疑气泡）。
  11. **演练筛选**：``drill:true`` 行默认隐藏；源筛选切「演练数据」后可见。

缺 playwright / 实例不可达 → SKIP exit 0（挂 gate_sweep -Full 的前提）；
无 token → ABORT exit 2。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：Windows 先试 ::1 每连接吃 ~2s 回退
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

XSS_TEXT = '<img src=x onerror="window.__xss=1">"\'><svg/onload=window.__xss2=1>'


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
        self.skipped: List[str] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  [SKIP] {name}  {why}")

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== 案例跟进页验证: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def _mock_rows(now: float) -> List[Dict[str, Any]]:
    """合成四张案例卡：XSS 未认领、危机已认领、已结案、演练残影。"""
    return [
        {
            "case_id": "CASE-GATE01-11111", "user_id": "gate_user_1",
            "chat_id": "123", "chat_title": "Alice",
            "source": "human_request", "severity": 2,
            "reason": "GATE-REASON-1", "created_at": now - 1800,
            "signal_count": 2, "intent_chain": ["greeting", "complaint"],
            "pattern": "", "pattern_desc": "", "satisfaction": 42,
            "at_risk": False, "consecutive_same": 1,
            "last_message": XSS_TEXT, "last_reply": "ok",
            "last_active": now - 1800, "escalation": False,
            "claimed_by": "", "claimed_at": 0,
            "closed": False, "closed_at": 0, "resolution": "",
            "note": "", "conv_ref": "telegram:default:123", "age_hours": 0.5,
            "platform": "telegram", "account_id": "8244899900",
            "peer_name": "Alice", "drill": False, "anchor_mid": "4242",
            "events": [{"ts": now - 1800, "code": "case.reason.human_request",
                        "quote": "转人工", "mid": "4242", "label": "GATE-REASON-1"}],
            "last_signal_at": now - 1800,
        },
        {
            "case_id": "CASE-GATE02-22222", "user_id": "gate_user_2",
            "chat_id": "456", "chat_title": "",
            "source": "crisis", "severity": 3,
            "reason": "GATE-REASON-2", "created_at": now - 7200,
            "signal_count": 1, "intent_chain": [],
            "pattern": "", "pattern_desc": "", "satisfaction": 80,
            "at_risk": True, "consecutive_same": 0,
            "last_message": "help", "last_reply": "here",
            "last_active": now - 7200, "escalation": True,
            "claimed_by": "gate_owner", "claimed_at": now - 3600,
            "closed": False, "closed_at": 0, "resolution": "",
            "note": "gate note", "conv_ref": "line:a1:U9", "age_hours": 2.0,
            "platform": "line", "account_id": "a1", "peer_name": "Bob",
            "drill": False, "anchor_mid": "", "events": [],
            "last_signal_at": now - 7200,
        },
        {
            "case_id": "CASE-GATE03-33333", "user_id": "gate_user_3",
            "chat_id": "789", "chat_title": "",
            "source": "media_complaint", "severity": 2,
            "reason": "GATE-REASON-3", "created_at": now - 90000,
            "signal_count": 1, "intent_chain": [],
            "pattern": "", "pattern_desc": "", "satisfaction": 80,
            "at_risk": False, "consecutive_same": 0,
            "last_message": "old", "last_reply": "old",
            "last_active": now - 90000, "escalation": False,
            "claimed_by": "gate_owner", "claimed_at": now - 90000,
            "closed": True, "closed_at": now - 86400, "resolution": "误报",
            "resolution_bucket": "false_alarm",
            "mute_until": now + 6 * 3600,
            "note": "", "conv_ref": "", "age_hours": 25.0,
            "platform": "", "account_id": "", "peer_name": "",
            "drill": False, "anchor_mid": "", "events": [],
            "last_signal_at": now - 90000,
        },
        {
            "case_id": "CASE-001004-67235", "user_id": "990001004",
            "chat_id": "990001004", "chat_title": "duel",
            "source": "media_complaint", "severity": 2,
            "reason": "GATE-DRILL", "created_at": now - 3600,
            "signal_count": 1, "intent_chain": [],
            "pattern": "", "pattern_desc": "", "satisfaction": 80,
            "at_risk": False, "consecutive_same": 0,
            "last_message": "你不是说发了？", "last_reply": "嗯",
            "last_active": now - 3600, "escalation": False,
            "claimed_by": "", "claimed_at": 0,
            "closed": False, "closed_at": 0, "resolution": "",
            "note": "", "conv_ref": "telegram:8244899900:990001004",
            "age_hours": 1.0,
            "platform": "telegram", "account_id": "8244899900",
            "peer_name": "duel", "drill": True, "anchor_mid": "99",
            "events": [{"ts": now - 3600, "code": "case.reason.media_lie_caught",
                        "quote": "你不是说发了？", "mid": "99"}],
            "last_signal_at": now - 3600,
        },
    ]


def _route_cases(page: Any, rows: List[Dict[str, Any]]) -> None:
    # P4 契约：/active 带 summary（媒体超龄 / 演练残影 pill 的数据源）
    summary = {
        "open": 2, "urgent": 1, "media_open": 1, "media_stale": 1,
        "media_stale_hours": 4.0, "oldest_media_hours": 5.0, "open_drill": 1,
    }
    # P7 契约：复发回环 alerts（建议条）+ 人工接管闭环（pill）
    effectiveness = {
        "source": "media_complaint", "window_days": 7.0, "lookback_days": 30.0,
        "closed": 4, "recurred": 3,
        "by_bucket": {"soothed": {"closed": 4, "recurred": 3, "immature": 0,
                                  "sample_uids": ["gate_user_1"]}},
        "alerts": [{"bucket": "soothed", "recurred": 3, "matured": 4,
                    "rate": 0.75, "sample_uids": ["gate_user_1"]}],
    }
    takeover_stats = {"count": 2, "open": 1, "median_hours": 2.5,
                      "oldest_open_hours": 3.0}
    body = json.dumps(
        {"cases": rows, "count": len(rows), "summary": summary,
         "effectiveness": effectiveness, "takeover_stats": takeover_stats},
        ensure_ascii=False)

    def _fulfill(route: Any) -> None:
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/cases/active*", _fulfill)


def _goto_cases(page: Any, base: str) -> bool:
    page.goto(base + "/cases", wait_until="domcontentloaded")
    try:
        page.wait_for_function("() => typeof loadCases === 'function'", timeout=20000)
    except Exception:
        return False
    page.wait_for_timeout(800)
    return True


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    now = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1360, "height": 900})
        ctx.request.post(base + "/login", form={"auth_token": token})

        # ── 1-6. 合成数据：卡片语义 / XSS / 认领 / 折叠 / 筛选 / 模态 ──────
        print("== 1. 三张合成案例卡（route mock，零生产写入）==")
        page = ctx.new_page()
        _route_cases(page, _mock_rows(now))
        if not _goto_cases(page, base):
            print("[SKIP] /cases 脚本未就绪（实例在重启？）")
            browser.close()
            return 0

        snap = page.evaluate(
            """() => {
              csSetFilter('all');   // 页面默认「待处理」；断言全集先切全部
              const cards = [...document.querySelectorAll('#case-list .case-card')];
              const by = {};
              cards.forEach(c => { by[c.getAttribute('data-cid')] = {
                cls: c.className,
                badges: [...c.querySelectorAll('.badge')].map(b => b.textContent.trim()),
                claimBtns: c.querySelectorAll("button[onclick^='csClaimIdx']").length,
                releaseBtns: c.querySelectorAll("button[onclick^='csReleaseIdx']").length,
                openConv: c.querySelectorAll("button[onclick^='csOpenConvIdx']").length,
                bubbleImgs: c.querySelectorAll('.case-bubbles img,.case-bubbles svg').length,
                bubbleUser: (c.querySelector('.bubble.user') || {}).textContent || '',
                bubblesVisible: c.querySelector('.case-bubbles')
                  ? getComputedStyle(c.querySelector('.case-bubbles')).display !== 'none' : null,
              }; });
              return {n: cards.length, by,
                      xss1: window.__xss === undefined,
                      xss2: window.__xss2 === undefined};
            }""")
        by = snap.get("by") or {}
        r1 = by.get("CASE-GATE01-11111") or {}
        r2 = by.get("CASE-GATE02-22222") or {}
        r3 = by.get("CASE-GATE03-33333") or {}
        # 默认过滤掉 drill → 只渲染 3 张真实案例
        ck.check("渲染 3 张真实案例卡（演练默认隐藏）",
                 snap.get("n") == 3, f"n={snap.get('n')}")
        ck.check("XSS：onerror 未执行", snap.get("xss1") and snap.get("xss2"))
        ck.check("XSS：气泡内零 img/svg 元素", r1.get("bubbleImgs") == 0,
                 f"imgs={r1.get('bubbleImgs')}")
        ck.check("XSS：载荷以文本原样呈现",
                 "onerror" in (r1.get("bubbleUser") or ""))
        ck.check("severity 色缘 class（sev-2 / sev-3）",
                 "sev-2" in (r1.get("cls") or "") and "sev-3" in (r2.get("cls") or ""))
        ck.check("未认领行出认领按钮（claimed_by 字段自适配）",
                 r1.get("claimBtns") == 1 and r1.get("releaseBtns") == 0)
        ck.check("已认领行出释放按钮 + 认领人徽章",
                 r2.get("releaseBtns") == 1
                 and any("gate_owner" in b for b in (r2.get("badges") or [])))
        ck.check("conv_ref 才出「打开会话」（r1/r2 有、r3 无）",
                 r1.get("openConv") == 1 and (r3.get("openConv") or 0) == 0)
        ck.check("已结案卡折叠（气泡隐藏）", r3.get("bubblesVisible") is False)

        ident = page.evaluate(
            """() => {
              const c = document.querySelector(".case-card[data-cid='CASE-GATE01-11111']");
              if (!c) return {};
              const user = c.querySelector('.case-user');
              const t = (user && user.textContent) || '';
              return {
                plat: !!c.querySelector('.plat-badge'),
                acct: !!c.querySelector('.case-acct') && /899900/.test(t),
                peer: /Alice/.test(t),
              };
            }""")
        ck.check("身份行：平台徽标", ident.get("plat"), str(ident))
        ck.check("身份行：账号短码", ident.get("acct"), str(ident))
        ck.check("身份行：客户名", ident.get("peer"), str(ident))

        midUrl = page.evaluate(
            """() => {
              // 不真开窗：拦 __openUniqueUrl，只读拼出的 URL
              let captured = '';
              const prev = window.__openUniqueUrl;
              window.__openUniqueUrl = function(url){ captured = String(url||''); return true; };
              try {
                csSetFilter('all');
                csOpenConvIdx(0);
              } finally { window.__openUniqueUrl = prev; }
              return captured;
            }""")
        ck.check("打开会话深链含 &mid=",
                 "mid=4242" in (midUrl or "") and "conv=" in (midUrl or ""),
                 str(midUrl)[:120])

        drillN = page.evaluate(
            """() => {
              const sel = document.getElementById('cs-src-filter');
              if (!sel || typeof csSetSrc !== 'function') return -1;
              sel.value = 'drill';
              csSetSrc('drill');
              csSetFilter('all');   // 演练卡未结案；all 口径最稳
              return document.querySelectorAll('#case-list .case-card').length;
            }""")
        ck.check("演练筛选后可见 drill 卡", drillN == 1, f"n={drillN}")
        page.evaluate(
            """() => {
              const sel = document.getElementById('cs-src-filter');
              if (sel) sel.value = 'all';
              csSetSrc('all');
              csSetFilter('all');
            }""")

        expanded = page.evaluate(
            """() => {
              const c = document.querySelector(".case-card[data-cid='CASE-GATE03-33333']");
              c.click();
              return getComputedStyle(c.querySelector('.case-bubbles')).display !== 'none';
            }""")
        ck.check("点击已结案卡展开", expanded)

        counts = page.evaluate(
            """() => ({all: document.getElementById('cs-n-all').textContent,
                       open: document.getElementById('cs-n-open').textContent,
                       closed: document.getElementById('cs-n-closed').textContent})""")
        ck.check("chips 计数 3/2/1",
                 counts == {"all": "3", "open": "2", "closed": "1"}, str(counts))

        vis = page.evaluate(
            """() => { csSetFilter('closed');
                       return document.querySelectorAll('#case-list .case-card').length; }""")
        ck.check("「已结案」筛选后仅 1 卡", vis == 1, f"n={vis}")
        hits = page.evaluate(
            """() => { csSetFilter('all'); csSetQ('GATE02');
                       return document.querySelectorAll('#case-list .case-card').length; }""")
        ck.check("搜索案号命中 1 卡", hits == 1, f"n={hits}")
        page.evaluate("() => csSetQ('')")

        # ── P4/P5：summary pill + 结案分桶徽章 + 静默标注 ──────────────
        p45 = page.evaluate(
            """() => {
              csSetFilter('all');
              const pill = id => {
                const el = document.getElementById(id);
                return el ? {shown: el.style.display !== 'none',
                             n: (el.querySelector('strong')||{}).textContent||''} : null;
              };
              const closed = document.querySelector(".case-card[data-cid='CASE-GATE03-33333']");
              const badges = closed
                ? [...closed.querySelectorAll('.badge-res')].map(b => b.textContent.trim()) : [];
              const line = closed ? (closed.querySelector('.closed-line')||{}).textContent||'' : '';
              return {media: pill('cs-media-stale-pill'), drill: pill('cs-drill-pill'),
                      resBadges: badges, mutedLine: line};
            }""")
        media_pill = p45.get("media") or {}
        drill_pill = p45.get("drill") or {}
        ck.check("summary→媒体超龄 pill 亮起（n=1）",
                 media_pill.get("shown") and media_pill.get("n") == "1",
                 str(media_pill))
        ck.check("summary→演练残影 pill 亮起（n=1）",
                 drill_pill.get("shown") and drill_pill.get("n") == "1",
                 str(drill_pill))
        ck.check("已结案卡出结案分桶徽章", len(p45.get("resBadges") or []) == 1,
                 str(p45.get("resBadges")))
        ck.check("已结案卡标注「同类静默至」",
                 "静默" in (p45.get("mutedLine") or "")
                 or "muted" in (p45.get("mutedLine") or "").lower(),
                 (p45.get("mutedLine") or "")[:80])

        # ── P7：复发建议条 + 人工接管 pill ─────────────────────────────
        p7 = page.evaluate(
            """() => {
              const adv = document.getElementById('cs-advice');
              const tk = document.getElementById('cs-takeover-pill');
              return {advShown: adv && adv.style.display !== 'none',
                      advText: adv ? adv.textContent : '',
                      tkShown: tk && tk.style.display !== 'none',
                      tkVal: (document.getElementById('cs-takeover-val')||{}).textContent||''};
            }""")
        ck.check("复发建议条亮起（3/4 · 75%）",
                 p7.get("advShown") and "3/4" in (p7.get("advText") or "")
                 and "75%" in (p7.get("advText") or ""),
                 (p7.get("advText") or "")[:90])
        ck.check("人工接管 pill（次数+中位+进行中）",
                 p7.get("tkShown") and "2" in (p7.get("tkVal") or "")
                 and "2.5" in (p7.get("tkVal") or ""),
                 str(p7.get("tkVal")))

        # ── P8：建议条点击 → 复发会话过滤（chip 可清） ─────────────────
        p8 = page.evaluate(
            """() => {
              csSetFilter('all');
              const before = document.querySelectorAll('#case-list .case-card').length;
              csAdvFilter(0);
              const cards = [...document.querySelectorAll('#case-list .case-card')];
              const filtered = cards.length;
              const cid = cards.length ? cards[0].getAttribute('data-cid') : '';
              const chip = !!document.querySelector('#cs-advice .cs-chip');
              csAdvClear();
              csSetFilter('all');
              const after = document.querySelectorAll('#case-list .case-card').length;
              return {before, filtered, cid, chip, after};
            }""")
        ck.check("建议条点击→只剩复发会话（gate_user_1）",
                 p8.get("filtered") == 1
                 and p8.get("cid") == "CASE-GATE01-11111", str(p8))
        ck.check("过滤中出可清 chip；清除后恢复",
                 p8.get("chip") and p8.get("after") == p8.get("before"),
                 str(p8))

        print("== 2. 结案模态（只验开/关，不确认）==")
        modal = page.evaluate(
            """() => {
              csSetFilter('open');
              closeCaseIdx(0);
              const m = document.getElementById('cs-close-modal');
              const shown = m.style.display !== 'none';
              const q = m.querySelector('.cs-quick button');
              q.click();
              const filled = document.getElementById('cs-close-res').value.length > 0;
              const activeN = m.querySelectorAll('.cs-quick button.active').length;
              /* P6：转人工标签 → 接管勾选行亮出（行有 conv_ref 才有意义） */
              const ho = m.querySelector(".cs-quick button[data-bucket='handoff']");
              ho.click();
              const toRow = document.getElementById('cs-takeover-row');
              const takeoverShownOnHo = toRow && toRow.style.display !== 'none';
              /* P5：误报标签 → 静默勾选行亮出（切标签时接管行应收起）；
                 手改文本 → 高亮与勾选一并收起 */
              const fa = m.querySelector(".cs-quick button[data-bucket='false_alarm']");
              fa.click();
              const muteRow = document.getElementById('cs-mute-row');
              const muteShownOnFa = muteRow && muteRow.style.display !== 'none';
              const takeoverHiddenOnFa = toRow && toRow.style.display === 'none';
              const ta = document.getElementById('cs-close-res');
              ta.value += 'x';
              ta.dispatchEvent(new Event('input', {bubbles: true}));
              const activeAfterEdit = m.querySelectorAll('.cs-quick button.active').length;
              const muteHiddenAfterEdit = muteRow && muteRow.style.display === 'none';
              csCloseCancel();
              return {shown, filled, activeN, takeoverShownOnHo, muteShownOnFa,
                      takeoverHiddenOnFa, activeAfterEdit, muteHiddenAfterEdit,
                      hidden: m.style.display === 'none'};
            }""")
        ck.check("模态打开", modal.get("shown"))
        ck.check("快捷标签回填结案原因", modal.get("filled"))
        ck.check("快捷标签点选高亮（覆盖式单选）", modal.get("activeN") == 1)
        ck.check("转人工标签亮出「接管」勾选（行有 conv_ref）",
                 modal.get("takeoverShownOnHo"))
        ck.check("误报标签亮出「同类静默」勾选", modal.get("muteShownOnFa"))
        ck.check("切标签→接管勾选收起（互斥不残留）",
                 modal.get("takeoverHiddenOnFa"))
        ck.check("手改文本→高亮/勾选一并收起",
                 modal.get("activeAfterEdit") == 0
                 and modal.get("muteHiddenAfterEdit"))
        ck.check("取消关闭模态", modal.get("hidden"))

        print("== 3. 编辑保护（未保存备注 → 本轮刷新跳过）==")
        paused = page.evaluate(
            """async () => {
              const inp = document.querySelector('.case-note-input');
              inp.focus(); inp.value = 'typing-in-progress';
              await loadCases();
              return {paused: document.getElementById('cs-paused').style.display !== 'none',
                      kept: document.querySelector('.case-note-input').value};
            }""")
        ck.check("亮「已暂停自动刷新」提示", paused.get("paused"))
        ck.check("输入内容未被重绘吞掉",
                 paused.get("kept") == "typing-in-progress", str(paused.get("kept")))
        page.close()

        # ── 4. 空态教育（0 案例）───────────────────────────────────────
        print("== 4. 空态教育 + 示例卡 ==")
        page2 = ctx.new_page()
        _route_cases(page2, [])
        if _goto_cases(page2, base):
            edu = page2.evaluate(
                """() => ({empty: !!document.querySelector('#case-list .cs-empty'),
                           bullets: document.querySelectorAll('#case-list .cs-empty ul li').length,
                           sample: !!document.querySelector('#case-list .cs-sample'),
                           ribbon: !!document.querySelector('#case-list .cs-ribbon')})""")
            ck.check("空态出现教育块", edu.get("empty"))
            ck.check("立案时机列表 5 条", edu.get("bullets") == 5,
                     f"n={edu.get('bullets')}")
            ck.check("示例卡带缎带", edu.get("sample") and edu.get("ribbon"))
        else:
            ck.skip("空态教育", "/cases 二次加载未就绪")
        page2.close()
        browser.close()

    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="案例跟进页门禁（route mock 合成数据，零生产写入）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[SKIP] 未安装 playwright（pip install playwright）")
        return 0

    token = args.token or read_token(args.data_root)
    if not token:
        print("[ABORT] 读不到 web_admin.auth_token（--token 或 --data-root）")
        return 2

    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception as e:
        print(f"[SKIP] 实例不可达 {args.base}: {e}")
        return 0

    try:
        return run(args.base, token, headed=args.headed)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 执行异常: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
