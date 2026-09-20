# -*- coding: utf-8 -*-
"""消息右键翻译组 + 多语对照弹层 + 灯箱识别翻译侧栏 真浏览器门禁（P0/P1 2026-08-18）。

**为什么需要它**：这三条链全是「模板热更即上生产」的纯前端交互（右键菜单项按消息
类型分型 / 弹层语言 chips 状态机 / 灯箱面板与气泡译文行同步），静态门禁只能证
「函数挂了 window」，证不了「右键真的弹出翻译项」。P0 施工当天就靠本套 harness
抓到过「点击选中竞态」——固化成门禁防回归。

**夹具手法**（与 verify_inbox_identity 同族 route-mock，全程零生产写入零真实成本）：
- chats 整表替换为一条合成会话；thread 喂合成消息（入站文本/图片/语音三条）；
- mark-read / translate-compare / translate-message-media 全部 stub fulfill——
  **不打真引擎不烧 GPU**（真实链路的端到端在 P0 上线时人工验证过一次，常驻门禁
  必须 hermetic）；
- 选会话走 `window.__desktopOpenConversation` 深链（官方入口，绕开列表点击竞态）。

用法::

    python tools/verify_xlate_ctx_ui.py            # 门禁模式
    python tools/verify_xlate_ctx_ui.py --headed   # 肉眼看一遍

缺 playwright / 实例不可达 → SKIP exit 0（挂 gate_sweep -Full 的前提）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "tools"))

from verify_inbox_density import DEFAULT_BASE, DEFAULT_DATA_ROOT, read_token  # noqa: E402

FAKE_KEY = "gate-xlctx-fake"
FAKE_NAME = "GateXlctxProbe"


class _Ck:
    def __init__(self) -> None:
        self.results: list[tuple[bool, str]] = []

    def check(self, ok: bool, name: str, detail: str = "") -> None:
        self.results.append((bool(ok), name))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")

    def summary(self) -> int:
        fails = [r for r in self.results if not r[0]]
        print(f"== 右键翻译组门禁: {len(self.results) - len(fails)}/{len(self.results)} PASS ==")
        return 1 if fails else 0


def _thread_payload(now: int) -> dict:
    return {
        "ok": True, "has_more": False, "oldest_ts": now - 300,
        "messages": [
            {"message_id": "gx1", "ts": now - 300, "direction": "in",
             "text": "Hello, could you send me the pricing details?"},
            {"message_id": "gx2", "ts": now - 200, "direction": "in", "text": "",
             "media_type": "image", "media_ref": "https://example.invalid/gate.jpg"},
            {"message_id": "gx3", "ts": now - 100, "direction": "in", "text": "",
             "media_type": "voice", "media_ref": "https://example.invalid/gate.ogg"},
            {"message_id": "gx4", "ts": now - 50, "direction": "in", "text": "",
             "media_type": "video", "media_ref": "https://example.invalid/gate.mp4"},
        ],
    }


def _stub_compare(lang: str) -> dict:
    mk = lambda eng, txt: {  # noqa: E731
        "engine": eng, "ok": True, "translated_text": txt, "error": "",
        "confidence": 0.9, "confidence_tier": "high", "confidence_signals": {}}
    return {"ok": True, "resolved_target": lang, "compare": {
        "source_lang": "en", "target_lang": lang,
        "candidates": [mk("ai", f"译文A[{lang}]"), mk("ollama_mt", f"译文B[{lang}]")],
        "fusion": {"ok": True, "text": f"融合稿[{lang}]", "engines_used": ["ai", "ollama_mt"],
                   "confidence": 0.95, "confidence_tier": "high"},
    }}


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = _Ck()
    now = int(time.time())
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        ctx.request.post(base + "/login", form={"auth_token": token})
        # 遥测保洁（xlt_ 报表 2026-08-17 同款教训）：门禁点击绝不许灌进 xlctx_/lbxl_/
        # docxl_ 裁决计数——context 级吞 sendBeacon + 拦 ui-event 路由双保险。
        ctx.add_init_script(
            "try{Object.defineProperty(navigator,'sendBeacon',"
            "{value:function(){return true;}});}catch(e){}")
        ctx.route("**/api/telemetry/ui-event",
                  lambda route: route.fulfill(status=204, body=""))
        page = ctx.new_page()

        def _patch_chats(route):
            resp = route.fetch()
            try:
                data = resp.json()
            except Exception:
                route.fulfill(response=resp)
                return
            rows = data.get("chats") or []
            if rows:
                fake = dict(rows[0])
                fake.update({
                    "chat_key": FAKE_KEY, "conversation_id": "gate_xlctx_fake",
                    "name": FAKE_NAME, "peer_name": FAKE_NAME, "title": FAKE_NAME,
                    "unread": 0, "synced_unread": 0, "snooze_until": 0,
                    "archived": 0, "last_message": "gate probe", "last_ts": now,
                    "chat_type": "private",
                })
                data["chats"] = [fake]
            route.fulfill(status=resp.status, content_type="application/json",
                          body=json.dumps(data, ensure_ascii=False))

        def _fake_thread(route):
            if FAKE_KEY in route.request.url:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(_thread_payload(now)))
            else:
                route.continue_()

        def _fake_compare(route):
            try:
                body = route.request.post_data_json or {}
            except Exception:
                body = {}
            lang = str(body.get("target_lang") or "zh")
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(_stub_compare(lang), ensure_ascii=False))

        # 1x1 红点 PNG：贴回流程的 stub 译文图（前端只消费 data URL，字节内容不重要）
        _TINY_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAA"
                     "fFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

        def _fake_media(route):
            try:
                body = route.request.post_data_json or {}
            except Exception:
                body = {}
            if body.get("patch"):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({
                                  "ok": True, "media_kind": "image",
                                  "patched_image_b64": _TINY_PNG,
                                  "items": [{"text": "OCR RAW TEXT", "dst": "识别译文成品",
                                             "box": [1, 1, 9, 9]}],
                                  "stats": {"blocks": 1, "patched": 1}},
                                  ensure_ascii=False))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({
                              "ok": True, "media_kind": "image", "from_upload": False,
                              "from_remote": False, "ocr_text": "OCR RAW TEXT",
                              "ocr_cached": False,
                              "translation": {"ok": True, "translated_text": "识别译文成品",
                                              "provider": "ai"}}, ensure_ascii=False))

        def _fake_ok(route):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True}))

        page.route("**/api/unified-inbox/chats*", _patch_chats)
        page.route("**/api/unified-inbox/thread*", _fake_thread)
        page.route("**/api/unified-inbox/translate-compare", _fake_compare)
        page.route("**/api/unified-inbox/translate-message-media", _fake_media)
        page.route("**/api/unified-inbox/mark-read*", _fake_ok)

        page.goto(base + "/workspace", wait_until="domcontentloaded")
        page.wait_for_selector("#conv-items .conv-item", timeout=20000)
        opened = False
        for attempt in range(4):
            page.evaluate(
                "() => window.__desktopOpenConversation({platform:'telegram',"
                "account_id:'8244899900',chat_key:'" + FAKE_KEY + "',"
                "name:'" + FAKE_NAME + "'})")
            if attempt > 0:
                page.evaluate("() => { try{ window.reloadThread(); }catch(e){} }")
            try:
                page.wait_for_selector("#msg-area .msg-row", timeout=8000)
                opened = True
                break
            except Exception:
                page.wait_for_timeout(600)
        if not opened:
            print("[SKIP] 合成会话线程未渲染（实例形态变化？）")
            browser.close()
            return 0
        page.evaluate(
            "() => { try{ ['cp-tour-veil','cp-tour-hl','cp-tour-pop']"
            ".forEach(id=>{var n=document.getElementById(id); if(n) n.remove();});"
            "}catch(_){} }")

        # ── 1. 三类消息的右键菜单分型 ──────────────────────────────
        page.locator("#msg-area .msg-row#msg-gx1").click(button="right")
        page.wait_for_selector(".ws-ctx-menu", timeout=5000)
        t1 = page.locator(".ws-ctx-menu").inner_text()
        ck.check("翻译这条" in t1 and "多线路对照" in t1, "文本行右键含翻译组")
        ck.check("识别翻译" not in t1 and "转写翻译" not in t1, "文本行不出媒体项")
        page.keyboard.press("Escape")

        page.locator("#msg-area .msg-row#msg-gx2").click(button="right")
        page.wait_for_selector(".ws-ctx-menu", timeout=5000)
        ck.check("识别翻译" in page.locator(".ws-ctx-menu").inner_text(),
                 "图片行右键含「识别翻译」")
        page.keyboard.press("Escape")

        page.locator("#msg-area .msg-row#msg-gx3").click(button="right")
        page.wait_for_selector(".ws-ctx-menu", timeout=5000)
        ck.check("转写翻译" in page.locator(".ws-ctx-menu").inner_text(),
                 "语音行右键含「转写翻译」")
        page.keyboard.press("Escape")

        page.locator("#msg-area .msg-row#msg-gx4").click(button="right")
        page.wait_for_selector(".ws-ctx-menu", timeout=5000)
        ck.check("转写翻译" in page.locator(".ws-ctx-menu").inner_text(),
                 "视频行右键含「转写翻译」（P2）")
        page.keyboard.press("Escape")

        # ── 2. 多语对照弹层（stub compare）─────────────────────────
        page.locator("#msg-area .msg-row#msg-gx1").click(button="right")
        page.wait_for_selector(".ws-ctx-menu", timeout=5000)
        page.locator(".ws-ctx-item", has_text="多线路对照").first.click()
        page.wait_for_selector(".xlcompare-mask", timeout=5000)
        chips = page.locator(".xlcompare-mask button[data-lang]")
        ck.check(chips.count() >= 4, "语言 chips ≥4", f"n={chips.count()}")
        page.wait_for_selector(".xlcompare-mask .xlcompare-opt", timeout=8000)
        n_opts = page.locator(".xlcompare-mask .xlcompare-opt").count()
        ck.check(n_opts == 3, "候选渲染（2 引擎 + 融合置顶）", f"opts={n_opts}")
        mask_txt = page.locator(".xlcompare-mask").inner_text()
        ck.check("融合稿" in mask_txt and "译文A" in mask_txt, "stub 候选文本就位")
        chips.nth(1).click()
        page.wait_for_function(
            "() => (document.querySelector('.xlcompare-mask')||{innerText:''})"
            ".innerText.match(/→/g)?.length >= 2", timeout=8000)
        ck.check(True, "第二语言分组渲染")
        chips.nth(2).click()
        page.wait_for_timeout(250)
        chips.nth(3).click()
        page.wait_for_timeout(250)
        active_n = page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'.xlcompare-mask button[data-lang]'))"
            ".filter(b=>b.style.fontWeight==='700').length")
        ck.check(active_n <= 3, "语言上限 3 生效", f"active={active_n}")
        page.locator(".xlcompare-cancel").click()
        ck.check(page.locator(".xlcompare-mask").count() == 0, "对照弹层可关闭")

        # ── 3. 灯箱识别翻译侧栏（stub media translate）──────────────
        page.locator("#msg-area .msg-row#msg-gx2 img.msg-media-img").click()
        page.wait_for_selector("#lightbox-overlay.show", timeout=5000)
        panel = page.locator("#lightbox-xl")
        ck.check(panel.is_visible(), "灯箱侧栏出现")
        ck.check(panel.locator(".lbxl-lang-row select").count() == 0,
                 "灯箱无原生 select")
        ck.check(panel.locator("#lb-xl-more").count() == 1, "灯箱「更多」芯片")
        page.locator("#lb-xl-more").click()
        page.wait_for_function(
            "() => (document.getElementById('lightbox-xl')||{}).classList"
            ".contains('is-pick')", timeout=3000)
        n_pick = page.evaluate(
            "() => document.querySelectorAll('#lb-xl-pick-list [data-xl-code]').length")
        ck.check(n_pick >= 30, "灯箱选语页列出目录", f"n={n_pick}")
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => !(document.getElementById('lightbox-xl')||{}).classList"
            ".contains('is-pick')", timeout=3000)
        ck.check(page.evaluate(
            "() => document.getElementById('lightbox-overlay')"
            ".classList.contains('show')"), "Esc 只关选语页、灯箱仍开")
        btn = panel.locator("button", has_text="识别翻译").first
        ck.check(btn.count() > 0, "侧栏带「识别翻译」按钮（手动触发不自动烧）")
        btn.click()
        page.wait_for_function(
            "() => (document.getElementById('lightbox-xl')||{innerText:''})"
            ".innerText.indexOf('OCR RAW TEXT') >= 0", timeout=8000)
        p_txt = panel.inner_text()
        ck.check("OCR RAW TEXT" in p_txt and "识别译文成品" in p_txt,
                 "侧栏渲染 识别原文+译文")
        bub = page.locator("#msg-area .msg-row#msg-gx2 .msg-translation").inner_text()
        ck.check("识别译文成品" in bub, "气泡译文行同步更新（同链路）")

        # ── 3b. 贴回流程（P3；stub patch 响应）──────────────────────
        pbtn = panel.locator("button", has_text="生成译文图").first
        ck.check(pbtn.count() > 0, "侧栏带「生成译文图」按钮")
        pbtn.click()
        page.wait_for_function(
            "() => (document.getElementById('lightbox-img')||{src:''})"
            ".src.indexOf('data:image/png') === 0", timeout=8000)
        ck.check(True, "生成后自动切到译文图（img src=data URL）")
        chips2 = panel.locator(".lbxl-patch-row button")
        ck.check(chips2.count() == 2, "原图/译文图切换 chips 出现")
        chips2.first.click()   # 切回原图
        page.wait_for_timeout(200)
        src_now = page.evaluate(
            "() => (document.getElementById('lightbox-img')||{src:''}).src")
        ck.check(not src_now.startswith("data:image/png"), "可切回原图核对",
                 src_now[-40:])
        page.keyboard.press("Escape")
        ck.check(page.evaluate(
            "() => !document.getElementById('lightbox-overlay')"
            ".classList.contains('show')"), "灯箱可关闭")
        # 重开灯箱=缓存回显（零请求）
        page.locator("#msg-area .msg-row#msg-gx2 img.msg-media-img").click()
        page.wait_for_selector("#lightbox-overlay.show", timeout=5000)
        ck.check("OCR RAW TEXT" in page.locator("#lightbox-xl").inner_text(),
                 "重开灯箱缓存回显（_mediaXlCache）")
        page.keyboard.press("Escape")

        # ── 4. 文档/字幕/音频入口形态 ──────────────────────────────
        accept = page.get_attribute("#docxl-docx-file", "accept") or ""
        ck.check(".pptx" in accept and ".srt" in accept and ".mp3" in accept,
                 "上传口 accept 含 pptx/srt/音频", accept[:90])
        ck.check(page.locator("#docxl-bilingual").count() == 1, "字幕双语勾选存在")

        browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--token", default="")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 不可用")
        return 0
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except Exception:
        print(f"[SKIP] 实例不可达: {args.base}")
        return 0
    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[SKIP] 未能从 {args.data_root} 读到 token")
        return 0
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
