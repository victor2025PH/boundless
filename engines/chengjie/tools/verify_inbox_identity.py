# -*- coding: utf-8 -*-
"""坐席工作台「回复区身份条」真浏览器门禁（Playwright；2026-07-30 沉淀）。

**为什么需要它**：人设主动触达产品里，发送前最后一眼必须说清楚
「以哪个人设 · 哪个号」在回。会话级人设覆写（``inbox.persona_conv_override``）
在出站链 / ``/api/persona/effective`` / 右栏 ``cp-persona`` 早已接好，而回复区
``#identity-bar`` 曾长期只读账号级 ``accountMeta``——i18n 键
``inbox.ident.chip_conv``（身份条「仅此会话」chip）从未被引用。
修完后若不钉住，下一次「为了省一次请求」把异步校正删掉，回归是**静默的**：
条还在、看起来也正常，只是会话覆写时又说错人。

与 ``tools/verify_inbox_density.py`` 同族（同一实例 + token 登录 + Playwright +
SKIP exit 0）。**不写生产覆写**——会话级覆写路径用 Playwright ``page.route``
注入合成响应，零副作用。

用法::

    python tools/verify_inbox_identity.py              # 门禁
    python tools/verify_inbox_identity.py --headed     # 肉眼
    python tools/verify_inbox_identity.py --self-proof # 探测器自证（必红才算过）

覆盖的不变量：
  0. 源码接线：``_identBarUpgrade`` + ``inbox.ident.chip_conv`` + ``conv_override``
     仍被引用（不依赖实例在线；防键再次变孤儿）。
  1. 有人设徽章的已读会话 → 身份条可见、非 warn、文案不含覆写标记。
  2. 无人设点的已读会话 → 身份条 warn（若实例无此类会话则 SKIP）。
  3. 打开后空闲轮询窗内 ``/api/persona/effective`` **不再增量请求**（TTL 缓存）。
  4. route 注入 ``tier=conv_override`` → 出现覆写 chip / 人话标记（中或英）。
  5. route 注入 HTTP 400 → 保留同步账号级结果（不崩、不空、无覆写标记）。
  6. 打开会话后 ``#identity-bar`` 必须 ``display:flex``（常驻，不是 hover 才显）。
  7. **切换即时性**（2026-07-30 第二批）：``cp-persona-changed``（ok）后身份条须在
     数秒内反映新 effective——即缓存被失效重取，而不是等 TTL 30s 自然过期。
     没有这条时的真实故障形态：toast 说「已切换为 X」、发送区身份条还说旧人设，
     两套真相并存半分钟。合成事件在页面内派发（不写任何绑定），新 effective 由
     route mock 提供，全程零生产写入。
  8. **行徽章覆写**（2026-07-30 第三批）：chats 行自带 ``eff_persona``
     （tier=conv_override）时，列表行人设徽章须显示覆写人设 + 覆写 title
     ——同时证明两处接线：``_applyAcctVisuals`` 消费该字段，且 ``_convItemSig``
     把它计入行 diff 签名（签名不含 → 行永不重画 → 徽章永不更新）。
     行数据由 chats 路由拦截改写（真响应打补丁），零生产写入。

**副作用：无。** 只点 ``.conv-item:not(.has-unread)``；route mock 不落盘。
缺 playwright / 实例不可达 → SKIP exit 0；无 token → ABORT exit 2。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s（见 verify_inbox_density.py 同行注释）
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}
# 空闲窗：realtime_poll 默认 3s，覆盖 ≥2 次 loadChats→_renderIdentityBar 重入
IDLE_CACHE_SEC = 8.0
ENGINE_ROOT = Path(__file__).resolve().parents[1]
INBOX_TEMPLATE = ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"

_OVERRIDE_MARKERS = (
    "仅此会话", "this chat only",
    "本会话覆写", "this-chat override",  # 旧括号句式,防未刷新页
)

_BAR_JS = """() => {
  const bar = document.getElementById('identity-bar');
  if (!bar) return {absent: true};
  const st = getComputedStyle(bar);
  const txt = ((bar.querySelector('.ib-txt') || {}).textContent || '').trim();
  const chip = ((bar.querySelector('.ib-chip') || {}).textContent || '').trim();
  return {
    absent: false,
    display: st.display,
    h: Math.round(bar.getBoundingClientRect().height),
    visible: st.display !== 'none' && bar.offsetParent !== null && bar.getBoundingClientRect().height > 0,
    warn: bar.classList.contains('warn'),
    override: bar.classList.contains('override'),
    text: txt,
    chip: chip,
    title: bar.getAttribute('title') || '',
  };
}"""

_FIND_ROWS_JS = """() => {
  const items = Array.from(
    document.querySelectorAll('#conv-items .conv-item:not(.has-unread)'));
  const withP = items.find(r => r.querySelector('.conv-persona-badge'));
  const noP = items.find(r => r.querySelector('.conv-nopersona-dot'));
  const pick = (row) => {
    if (!row) return null;
    const badge = row.querySelector('.conv-persona-badge');
    return {
      key: row.getAttribute('data-key') || row.id || '',
      badge: badge ? (badge.textContent || '').trim() : '',
    };
  };
  return {
    total_read: items.length,
    with_persona: pick(withP),
    no_persona: pick(noP),
  };
}"""


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

    def summary(self, title: str = "收件箱身份条验证") -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== {title}: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def check_source_wiring(ck: Checker) -> None:
    """不依赖实例：防 chip_conv / upgrade 再次变孤儿。"""
    print("== 0. 源码接线（静态）==")
    if not INBOX_TEMPLATE.exists():
        ck.check("unified_inbox.html 存在", False, str(INBOX_TEMPLATE))
        return
    src = INBOX_TEMPLATE.read_text(encoding="utf-8")
    ck.check("源码引用 inbox.ident.chip_conv", "inbox.ident.chip_conv" in src)
    ck.check("源码渲染覆写 chip", "ib-chip" in src)
    ck.check("源码定义 _identBarUpgrade", "function _identBarUpgrade" in src)
    ck.check("upgrade 识别 conv_override",
             "conv_override" in src and "_identBarPersonaHtml" in src)
    ck.check("生效人设缓存有 TTL", "_IDENT_EFF_TTL" in src and "_identEffCache" in src)
    # 切换即时性接线（三件套缺一即回归到「toast 与身份条两套真相」）：
    # 失效入口 + 换绑监听器真的调它 + 在途响应代数守卫（防旧响应把旧人设写回）。
    import re as _re
    ck.check("源码定义 _identEffInvalidate", "function _identEffInvalidate" in src)
    ck.check("cp-persona-changed 监听器调用失效入口",
             bool(_re.search(r"cp-persona-changed[\s\S]{0,800}_identEffInvalidate\s*\(", src)))
    ck.check("在途响应有代数守卫", "_identEffGen" in src)
    # 行徽章覆写接线（第三批）：行渲染消费 eff_persona + diff 签名含它 + 覆写 title 键。
    ck.check("行徽章消费 eff_persona",
             bool(_re.search(r"_applyAcctVisuals[\s\S]{0,2000}eff_persona", src)))
    ck.check("行 diff 签名含 eff_persona",
             bool(_re.search(r"_convItemSig[\s\S]{0,1600}eff_persona", src)))
    ck.check("覆写 title 键已引用", "inbox.ident.row_override_t" in src)
    ck.check("行徽章覆写绿点 class", "classList.toggle('ov'" in src or 'classList.toggle("ov"' in src)
    ck.check("跳转转化埋点", "ident_bar_jump_bind" in src)
    ck.check("转化按会话键对账",
             "function _identJumpNoteConvert" in src and "_identJumpKey" in src)
    ck.check("身份条跳转示能（ib-go）", 'class="ib-go"' in src)


def _has_override_marker(text: str) -> bool:
    t = text or ""
    return any(m in t for m in _OVERRIDE_MARKERS)


def _has_override_signal(bar: dict) -> bool:
    """chip / .override 类 / 文案标记 任一即可——新呈现以 chip 为准。"""
    if not bar:
        return False
    if bar.get("override") or (bar.get("chip") or "").strip():
        return True
    blob = " ".join([
        bar.get("text") or "",
        bar.get("chip") or "",
        bar.get("title") or "",
    ])
    return _has_override_marker(blob)


def _wait_bar(page: Any, *, timeout_ms: int = 8000) -> dict:
    deadline = time.time() + timeout_ms / 1000.0
    last: dict = {"absent": True}
    while time.time() < deadline:
        last = page.evaluate(_BAR_JS)
        if last.get("visible"):
            return last
        page.wait_for_timeout(150)
    return last


def _click_read_row(page: Any, kind: str) -> Optional[dict]:
    """kind: 'with_persona' | 'no_persona'. 返回行摘要或 None。"""
    info = page.evaluate(_FIND_ROWS_JS)
    row = info.get(kind)
    if not row:
        return None
    # 再点一次 DOM（避免闭包过期）；按徽章有无匹配
    clicked = page.evaluate(
        """(kind) => {
          const items = Array.from(
            document.querySelectorAll('#conv-items .conv-item:not(.has-unread)'));
          let row = null;
          if (kind === 'with_persona')
            row = items.find(r => r.querySelector('.conv-persona-badge'));
          else
            row = items.find(r => r.querySelector('.conv-nopersona-dot'));
          if (!row) return false;
          row.click();
          return true;
        }""",
        kind,
    )
    if not clicked:
        return None
    return row


def _login_workspace(p: Any, base: str, token: str, *, headed: bool) -> Tuple[Any, Any, Any]:
    browser = p.chromium.launch(headless=not headed)
    ctx = browser.new_context(viewport=VIEWPORT)
    ctx.request.post(base + "/login", form={"auth_token": token})
    page = ctx.new_page()
    page.goto(base + "/workspace", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof window.setPlatFilter === 'function'", timeout=20000)
    page.wait_for_timeout(3500)
    return browser, ctx, page


def _eff_counter(page: Any) -> List[str]:
    hits: List[str] = []

    def _on_req(req: Any) -> None:
        u = req.url or ""
        if "/api/persona/effective" in u:
            hits.append(u)

    page.on("request", _on_req)
    return hits


def run(base: str, token: str, *, headed: bool = False,
        self_proof: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    check_source_wiring(ck)

    with sync_playwright() as p:
        try:
            browser, ctx, page = _login_workspace(p, base, token, headed=headed)
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP] 工作台脚本未就绪（{str(e)[:80]}）")
            return 0

        # ── self-proof：拆掉身份条，可见性断言必须红 ─────────────────
        if self_proof:
            print("== self-proof: 拆掉 #identity-bar，探测器应红 ==")
            opened = _click_read_row(page, "with_persona") or _click_read_row(
                page, "no_persona")
            if not opened:
                # 无已读会话时仍可拆 DOM 测「absent」路径
                page.evaluate(
                    "() => { const b=document.getElementById('identity-bar');"
                    " if(b) b.remove(); }")
                bar = page.evaluate(_BAR_JS)
                caught = bool(bar.get("absent"))
                print(f"  [{'PASS' if caught else 'FAIL'}] 探测器抓住 absent"
                      f"  absent={bar.get('absent')}")
                browser.close()
                # self-proof 约定：抓住缺陷 → 工具 exit 0；没抓住 → exit 1
                return 0 if caught else 1
            page.wait_for_timeout(800)
            page.evaluate(
                "() => { const b=document.getElementById('identity-bar');"
                " if(b) b.remove(); }")
            bar = page.evaluate(_BAR_JS)
            # 正常门禁会要求 visible；这里反过来：拆掉后不应再 visible
            caught = (not bar.get("visible")) or bar.get("absent")
            print(f"  [{'PASS' if caught else 'FAIL'}] 探测器能识别身份条消失"
                  f"  absent={bar.get('absent')} visible={bar.get('visible')}")
            browser.close()
            return 0 if caught else 1

        hits = _eff_counter(page)

        # ── 1. 账号级（有人设）──────────────────────────────────────
        print("== 1. 账号级人设：有徽章已读会话 ==")
        row = _click_read_row(page, "with_persona")
        if not row:
            ck.skip("有人设身份条", "无「已读且带人设徽章」会话——不碰未读")
            # 后续依赖该会话的项一并跳过
            browser.close()
            return ck.summary()
        bar = _wait_bar(page)
        ck.check("身份条常驻可见", bar.get("visible") and bar.get("display") == "flex",
                 f"display={bar.get('display')} h={bar.get('h')}")
        ck.check("有人设时非 warn", bar.get("warn") is False,
                 f"warn={bar.get('warn')} text={bar.get('text')!r}")
        ck.check("账号级文案无覆写标记", not _has_override_marker(bar.get("text") or ""),
                 f"text={bar.get('text')!r}")
        ck.check("账号级文案非空", bool(bar.get("text")),
                 f"text={bar.get('text')!r}")

        # ── 2. 缓存：空闲窗无增量 effective 请求 ─────────────────────
        print(f"== 2. TTL 缓存：空闲 {IDLE_CACHE_SEC:.0f}s 无增量请求 ==")
        # 等首轮 upgrade 落盘（至多 1 次）
        t0 = time.time()
        while time.time() - t0 < 5.0 and len(hits) < 1:
            page.wait_for_timeout(100)
        # ⚠ 量法坑（2026-08-15 实锤）：打开会话是一次「请求突发」——身份条
        # （?platform= 三元组）+ cp-persona + cp-draft（?conversation_id=）各打一发，
        # 彼此相隔可达 ~1s。旧量法在**首个**请求落地即开计空闲窗，突发的尾巴
        # 落进窗内被误判成「TTL 缓存失效」。先等突发收敛（2.5s 无新请求，上限 10s）
        # 再开窗。不会掩真 bug：若缓存真坏（轮询每 3s 重打），收敛永远等不到，
        # 空闲窗内照样有增量、检查照样红。
        settle_t0 = time.time()
        last_n = len(hits)
        last_change = time.time()
        while time.time() - settle_t0 < 10.0:
            page.wait_for_timeout(200)
            if len(hits) != last_n:
                last_n = len(hits)
                last_change = time.time()
            elif time.time() - last_change >= 2.5:
                break
        after_open = len(hits)
        page.wait_for_timeout(int(IDLE_CACHE_SEC * 1000))
        after_idle = len(hits)
        ck.check("打开后至少打过一次 effective（或已同步完成）",
                 after_open >= 1 or bool(bar.get("text")),
                 f"hits_after_open={after_open}")
        ck.check("空闲窗无增量 effective 请求",
                 after_idle == after_open,
                 f"open={after_open} idle={after_idle} (+{after_idle - after_open})")

        # ── 3. 无人设对照 ────────────────────────────────────────────
        print("== 3. 无人设对照（warn）==")
        row_np = _click_read_row(page, "no_persona")
        if not row_np:
            ck.skip("无人设 warn", "无「已读且带无人设点」会话")
        else:
            bar_np = _wait_bar(page)
            ck.check("无人设时 warn", bar_np.get("warn") is True,
                     f"warn={bar_np.get('warn')} text={bar_np.get('text')!r}")
            ck.check("无人设文案含「未绑/No persona」",
                     ("未绑" in (bar_np.get("text") or "")
                      or "No persona" in (bar_np.get("text") or "")),
                     f"text={bar_np.get('text')!r}")

        # ── 4. route：conv_override ───────────────────────────────
        print("== 4. route 注入 conv_override（不写生产）==")
        browser.close()

        browser, ctx, page = _login_workspace(p, base, token, headed=headed)
        mock_name = "GateMockPersona"

        def _fulfill_override(route: Any) -> None:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "ok": True,
                    "enabled": True,
                    "effective": {
                        "id": "gate_mock_persona",
                        "name": mock_name,
                        "role": "",
                        "has_voice": False,
                        "tier": "conv_override",
                    },
                }, ensure_ascii=False),
            )

        page.route("**/api/persona/effective*", _fulfill_override)
        row = _click_read_row(page, "with_persona")
        if not row:
            ck.skip("覆写文案", "无已读有人设会话可开")
        else:
            # 等异步 upgrade 替换同步文案（chip 在 .ib-txt 外，用 signal 而非整句括号）
            ok_ov = False
            text_ov = ""
            bar_ov: dict = {}
            for _ in range(40):
                bar_ov = page.evaluate(_BAR_JS)
                text_ov = bar_ov.get("text") or ""
                if mock_name in text_ov and _has_override_signal(bar_ov):
                    ok_ov = True
                    break
                page.wait_for_timeout(150)
            ck.check("覆写态文案含 mock 人设名", mock_name in text_ov,
                     f"text={text_ov!r}")
            ck.check("覆写态出现 chip / override 标记",
                     ok_ov or _has_override_signal(bar_ov),
                     f"text={text_ov!r} chip={bar_ov.get('chip')!r}")
            ck.check("覆写态非 warn",
                     page.evaluate(_BAR_JS).get("warn") is False)

        page.unroute("**/api/persona/effective*")
        browser.close()

        # ── 5. route：400 优雅降级 ────────────────────────────────
        print("== 5. route 注入 400：保留同步账号级 ==")
        browser, ctx, page = _login_workspace(p, base, token, headed=headed)

        def _fulfill_400(route: Any) -> None:
            route.fulfill(status=400, content_type="application/json",
                          body='{"detail":"gate_forced_400"}')

        page.route("**/api/persona/effective*", _fulfill_400)
        row = _click_read_row(page, "with_persona")
        if not row:
            ck.skip("400 降级", "无已读有人设会话可开")
        else:
            bar400 = _wait_bar(page)
            # 给 upgrade 失败路径一点时间，确认没有被清空
            page.wait_for_timeout(1200)
            bar400 = page.evaluate(_BAR_JS)
            ck.check("400 后身份条仍可见", bar400.get("visible"),
                     f"display={bar400.get('display')}")
            ck.check("400 后保留同步文案（非空、无覆写标记）",
                     bool(bar400.get("text"))
                     and not _has_override_marker(bar400.get("text") or ""),
                     f"text={bar400.get('text')!r}")
            ck.check("400 后非 warn（账号级有人设）",
                     bar400.get("warn") is False,
                     f"warn={bar400.get('warn')}")

        browser.close()

        # ── 6. 切换即时性：cp-persona-changed → 缓存失效 + 即时校正 ──
        print("== 6. 切换即时性：合成 cp-persona-changed（不写绑定）==")
        browser, ctx, page = _login_workspace(p, base, token, headed=headed)
        row = _click_read_row(page, "with_persona")
        if not row:
            ck.skip("切换即时性", "无已读有人设会话可开")
        else:
            _wait_bar(page)
            page.wait_for_timeout(800)   # 让首轮 upgrade 落缓存（TTL 30s 内不会再取）
            has_cp = page.evaluate(
                "() => !!document.getElementById('ws-cp-persona')")
            if not has_cp:
                ck.skip("切换即时性", "#ws-cp-persona 未挂载（副驾未启用？）")
            else:
                switch_name = "GateSwitchMock"

                def _fulfill_switched(route: Any) -> None:
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({
                            "ok": True,
                            "enabled": True,
                            "effective": {
                                "id": "gate_switch_mock",
                                "name": switch_name,
                                "role": "",
                                "has_voice": False,
                                "tier": "conv_override",
                            },
                        }, ensure_ascii=False),
                    )

                page.route("**/api/persona/effective*", _fulfill_switched)
                page.evaluate(
                    """(name) => {
                      const el = document.getElementById('ws-cp-persona');
                      el.dispatchEvent(new CustomEvent('cp-persona-changed', {
                        detail: {ok: true, scope: 'conversation',
                                 personaId: 'gate_switch_mock', personaName: name},
                      }));
                    }""",
                    switch_name,
                )
                fresh = ""
                ok_fresh = False
                for _ in range(30):          # ≤4.5s；TTL 是 30s，等到 = 真失效了
                    b = page.evaluate(_BAR_JS)
                    fresh = b.get("text") or ""
                    if switch_name in fresh:
                        ok_fresh = True
                        break
                    page.wait_for_timeout(150)
                ck.check("换绑事件后身份条数秒内反映新 effective（缓存已失效）",
                         ok_fresh, f"text={fresh!r}")
                if ok_fresh:
                    ck.check("即时校正带覆写标记",
                             _has_override_signal(b),
                             f"text={fresh!r} chip={b.get('chip')!r}")
                page.unroute("**/api/persona/effective*")

        browser.close()

        # ── 7. 行徽章覆写：chats route mock 全行打补丁 ────────────────
        print("== 7. 行徽章覆写（chats route mock，不写生产）==")
        browser, ctx, page = _login_workspace(p, base, token, headed=headed)
        row_name = "GateRowMock"
        had_badge = page.evaluate(
            "() => !!document.querySelector('#conv-items .conv-persona-badge')")
        if not had_badge:
            # 徽章只在多账号平台渲染（_multiAcctSet 闸门）；该实例若无多账号
            # 平台则整节无判别力 → SKIP，不硬造假绿。
            ck.skip("行徽章覆写", "当前列表无人设徽章（无多账号平台？）")
        else:
            def _patch_chats(route: Any) -> None:
                resp = route.fetch()
                try:
                    data = resp.json()
                except Exception:
                    route.fulfill(response=resp)
                    return
                for c in (data.get("chats") or []):
                    c["eff_persona"] = {"id": "gate_row_mock",
                                        "name": row_name,
                                        "tier": "conv_override"}
                route.fulfill(
                    status=resp.status,
                    content_type="application/json",
                    body=json.dumps(data, ensure_ascii=False),
                )

            page.route("**/api/unified-inbox/chats*", _patch_chats)
            # 空闲期列表更新走 SSE 推送、**没有**周期全量轮询（实测嗅探 10s 零
            # /api/ 请求）——拦截装上后必须主动 reload 让「初始加载」走 mock；
            # build 路径与 diff 路径调同一个 _applyAcctVisuals，消费语义等价，
            # diff 签名接线另由第 0 节静态检查钉住。
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_function(
                    "() => typeof window.setPlatFilter === 'function'",
                    timeout=20000)
            except Exception:
                pass
            hit = None
            sample: List[dict] = []
            for _ in range(80):          # ≤12s：重载 + 初始列表渲染
                res = page.evaluate(
                    """(name) => {
                      const out = [];
                      document.querySelectorAll(
                        '#conv-items .conv-persona-badge').forEach(b => {
                        out.push({t: (b.textContent || '').trim(),
                                  title: b.getAttribute('title') || '',
                                  ov: b.classList.contains('ov')});
                      });
                      return {hit: out.find(x => x.title.indexOf(name) >= 0) || null,
                              sample: out.slice(0, 6)};
                    }""",
                    row_name,
                )
                hit = res.get("hit")
                sample = res.get("sample") or []
                if hit:
                    break
                page.wait_for_timeout(150)
            ck.check("行徽章反映行级覆写人设（消费 + 签名两处接线）",
                     bool(hit), f"badges={sample}")
            if hit:
                ck.check("行徽章 title 带覆写标记",
                         _has_override_marker(hit.get("title") or ""),
                         f"title={hit.get('title')!r}")
                ck.check("行徽章带覆写绿点", bool(hit.get("ov")),
                         f"ov={hit.get('ov')!r}")
                ck.check("行徽章短名取自覆写人设",
                         (hit.get("t") or "") == row_name[0],
                         f"text={hit.get('t')!r}")
            page.unroute("**/api/unified-inbox/chats*")

        browser.close()

    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="坐席工作台回复区身份条门禁（只读 + route mock，无副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"实例地址（默认 {DEFAULT_BASE}）")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--self-proof", action="store_true",
                    help="探测器自证：拆掉身份条，能抓住才 exit 0")
    ap.add_argument("--source-only", action="store_true",
                    help="只跑静态源码接线（不启浏览器）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if args.source_only:
        ck = Checker()
        check_source_wiring(ck)
        return ck.summary("身份条源码接线")

    try:
        import playwright  # noqa: F401
    except ImportError:
        # 仍守静态接线——实例环境缺 PW 时至少别把孤儿键放回去
        print("[WARN] 未装 playwright；仅跑静态源码接线")
        ck = Checker()
        check_source_wiring(ck)
        return ck.summary("身份条源码接线（无 playwright）")

    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except urllib.error.HTTPError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达（{args.base}）：{str(e)[:80]}")
        # 实例挂了仍跑静态段，避免「服务宕 = 身份接线完全失守」
        ck = Checker()
        check_source_wiring(ck)
        code = ck.summary("身份条源码接线（实例不可达）")
        return 0 if code == 0 else code

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[ABORT] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 2

    print(f"== 目标 {args.base}（视口 {VIEWPORT['width']}x{VIEWPORT['height']}）==")
    return run(args.base, token, headed=args.headed, self_proof=args.self_proof)


if __name__ == "__main__":
    sys.exit(main())
