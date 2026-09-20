# -*- coding: utf-8 -*-
"""语音「⋮ 更多」菜单 + 一键克隆绑定弹窗 真浏览器门禁（P0 2026-08-03 同批沉淀）。

**为什么需要它**：这批能力全是模板热更新直上生产的前端逻辑——静态门禁只能证
「函数挂了 window / i18n 键存在」，证不了「菜单真的弹得出来、弹窗默认值真的填对、
授权闸门真的拦得住」。与 ``verify_inbox_density.py`` / ``verify_account_rail_ui.py``
同族（同一实例 + token 登录 + Playwright + SKIP exit 0 语义）。

**副作用：无（刻意设计）。**
  * 只点 ``.conv-item:not(.has-unread)``（已读会话）找语音气泡，不改任何已读态；
  * 克隆弹窗只开到「表单就绪」并验证**授权未勾选时提交被前端闸门拦下**——
    这一步恰好是被测不变量本身，全程不发 /api/voice/enroll；
  * 倍速点击只改本 gate 的一次性浏览器 profile 的 localStorage，对坐席零影响。

用法::

    python tools/verify_voice_clone_ui.py            # 门禁模式
    python tools/verify_voice_clone_ui.py --headed   # 肉眼看一遍

覆盖的不变量：
  1. 静态骨架 + window 暴露：#voice-more-menu / #vcm-overlay 在 DOM，
     _vmOpen / _vRateApply 可达（IIFE 内定义必须显式挂 window）。
  2. 新样式已到浏览器（.vm-menu position:fixed —— 抓「改了 CSS 忘 bump ?v=」）。
  3. 入站语音气泡有「更多 ⋮」按钮；原生 <audio> 带 controlslist 收纳下载/倍速。
  4. 菜单弹出且含 克隆/倍速×5/下载/转写翻译/引用；倍速点击对全部语音生效并落
     localStorage。
  5. 克隆弹窗表单就绪：音色名预填、人设下拉有选项、授权默认未勾。
  6. 授权闸门：未勾选提交 → 前端拦下出错误提示（不发任何请求）。
  7. 弹窗可关闭（✕），遮罩收起。

token 从实例数据根读取，**绝不打印**。缺 playwright / 实例不可达 / 找不到语音
消息 → SKIP exit 0（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

try:  # Windows GBK 控制台遇 ⋮/中文会崩；gate_sweep 环境未知，兜底 UTF-8+replace
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_BASE = "http://127.0.0.1:18799"  # 127.0.0.1 而非 localhost：服务只听 IPv4，::1 回退每连接吃 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}
MAX_CONV_PROBE = 40   # 最多定向打开多少个已读会话找语音气泡（找不到=SKIP，不算失败）


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
        print(f"\n== 语音克隆 UI 验证: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def _voice_conv_ids(data_root: str) -> List[str]:
    """只读查实例 inbox.db，取近期含**带归档入站语音**的会话 conversation_id（新→旧）。

    ``mode=ro`` URI 零写风险（对齐 proactive_review 读 inbox.db 的口径）；
    conversation_id 与 DOM ``.conv-item[data-cid]`` 同格式（platform:account:chat_key），
    可精确定向（避免「同 chat_key 不同 account」误命中——诊断实测过这个坑）。
    库缺失/查询异常 → 返回 []（回落纯 DOM 遍历）。
    """
    import sqlite3
    fp = Path(data_root) / "config" / "inbox.db"
    if not fp.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{fp.as_posix()}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT c.conversation_id FROM messages m"
            " JOIN conversations c ON c.conversation_id = m.conversation_id"
            " WHERE m.direction='in' AND m.media_type IN ('voice','audio')"
            "   AND COALESCE(m.media_ref,'') <> ''"
            " GROUP BY m.conversation_id ORDER BY MAX(m.ts) DESC LIMIT 15").fetchall()
        con.close()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        return []


def _backfill_until_voice(page: Any, rounds: int = 16) -> bool:
    """在当前会话里点「加载更早/深度回填」按钮把历史语音拉进视口，直到出现
    .vm-more-btn 或到头。语音消息常在历史深处（首屏是最近文本），故必须回填。"""
    for _ in range(rounds):
        if page.evaluate("() => !!document.querySelector('#msg-area .vm-more-btn')"):
            return True
        did = page.evaluate(
            "() => { const a=document.getElementById('msg-area'); if(!a) return 0;"
            " const b=[...a.querySelectorAll('button.tiny-btn')].find(x =>"
            "   /older|deep|\\u66f4\\u65e9|\\u56de\\u586b|\\u624b\\u673a/"
            "     .test((x.getAttribute('onclick')||'')+(x.textContent||'')));"
            " if(b && !b.disabled){ b.click(); return 1; } return 0; }")
        page.wait_for_timeout(1400)
        if not did:
            break
    return page.evaluate("() => !!document.querySelector('#msg-area .vm-more-btn')")


def _find_voice_conv(page: Any, data_root: str) -> bool:
    """找一个带语音「更多 ⋮」按钮的会话；找到返回 True 并停在该会话。

    定位策略（零副作用——只打开会话 + 回填历史，不发任何写请求）：
      1. 切到「全部」tab；
      2. 优先用 inbox.db 只读查出的语音会话 conversation_id，DOM 按 data-cid 精确定向
         打开（诊断实测 ~17s 稳定命中）；每个打开后回填历史直到语音气泡浮现；
      3. DB 不可用/无语音时回落：盲翻已加载的已读会话（DOM data-cid，跳过未读免改已读态）。
    """
    page.evaluate(
        "() => { const t=document.querySelector(\".ftab[data-f='all']\");"
        " if (t && window.setFilter) setFilter('all', t); }")
    page.wait_for_timeout(800)
    # 主路径：DB 精确定向
    for cid in _voice_conv_ids(data_root)[:MAX_CONV_PROBE]:
        clicked = page.evaluate(
            "(cid) => { const el=document.querySelector"
            "('.conv-item[data-cid=\"'+(window.CSS?CSS.escape(cid):cid)+'\"]');"
            " if(!el) return false; el.click(); return true; }", cid)
        if not clicked:
            continue
        page.wait_for_timeout(1500)
        if _backfill_until_voice(page):
            return True
    # 回落：盲翻已加载的已读会话（DOM 顺序）
    read_cids = page.evaluate(
        "() => [...document.querySelectorAll('.conv-item:not(.has-unread)')]"
        " .map(e => e.getAttribute('data-cid')).filter(Boolean)")
    for cid in (read_cids or [])[:MAX_CONV_PROBE]:
        page.evaluate(
            "(cid) => { const el=document.querySelector"
            "('.conv-item[data-cid=\"'+(window.CSS?CSS.escape(cid):cid)+'\"]');"
            " if(el) el.click(); }", cid)
        page.wait_for_timeout(1300)
        if page.evaluate("() => !!document.querySelector('#msg-area .vm-more-btn')"):
            return True
    return False


def run(base: str, token: str, *, data_root: str = "", headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport=VIEWPORT)
        ctx.request.post(base + "/login", form={"auth_token": token})
        page = ctx.new_page()
        page.goto(base + "/workspace", wait_until="domcontentloaded")
        try:
            page.wait_for_function("() => typeof window.setPlatFilter === 'function'",
                                   timeout=20000)
        except Exception:
            print("[SKIP] 工作台脚本未就绪（实例在重启？）")
            browser.close()
            return 0
        page.wait_for_timeout(3000)

        print("== 1. 静态骨架 + window 暴露 + 样式到位 ==")
        ck.check("菜单/弹窗骨架在 DOM",
                 page.evaluate("() => !!document.getElementById('voice-more-menu')"
                               " && !!document.getElementById('vcm-overlay')"))
        ck.check("_vmOpen / _vRateApply 已挂 window",
                 page.evaluate("() => typeof window._vmOpen==='function'"
                               " && typeof window._vRateApply==='function'"))
        ck.check(".vm-menu 样式已生效（CSS ?v= 已到浏览器）",
                 page.evaluate("() => getComputedStyle(document.getElementById('voice-more-menu'))"
                               ".position === 'fixed'"))

        print("== 2. 定位一条带语音的会话（inbox.db 定向 + 历史回填）==")
        if not _find_voice_conv(page, data_root):
            ck.skip("语音气泡交互链", "当前实例无带归档语音的会话（或回填不到）")
            browser.close()
            return ck.summary()
        ck.check("入站语音气泡渲染「更多」按钮（在自定义播放器内）",
                 page.evaluate("() => !!document.querySelector('#msg-area .vp .vm-more-btn')"))
        # P1：自定义播放器取代原生 <audio controls> —— 结构齐全 + 真实 audio 视觉隐藏（但仍加载）
        ck.check("自定义语音播放器结构就绪（播放键/进度条/倍速/隐藏 audio）",
                 page.evaluate("() => { const vp=document.querySelector('#msg-area .vp'); if(!vp) return false;"
                               " const a=vp.querySelector('audio.vp-audio');"
                               " const hidden=a && (a.offsetWidth<=2 || getComputedStyle(a).opacity==='0');"
                               " return !!vp.querySelector('.vp-play') && !!vp.querySelector('.vp-track')"
                               " && !!vp.querySelector('.vp-rate') && !!a && !!hidden; }"))

        print("== 3. ⋮ 菜单：弹出 + 条目 + 倍速 ==")
        page.evaluate("() => document.querySelector('#msg-area .vm-more-btn').click()")
        page.wait_for_timeout(400)
        snap = page.evaluate(
            "() => { const m=document.getElementById('voice-more-menu');"
            " return { shown:m.classList.contains('show'),"
            " clone:!!m.querySelector('[data-act=clone]'),"
            " rates:m.querySelectorAll('.vm-rate').length,"
            " dl:!!m.querySelector('[data-act=dl]'),"
            " tx:!!m.querySelector('[data-act=tx]'),"
            " quote:!!m.querySelector('[data-act=quote]') }; }")
        ck.check("菜单弹出", snap["shown"])
        ck.check("菜单含 克隆/下载/转写翻译/引用",
                 snap["clone"] and snap["dl"] and snap["tx"] and snap["quote"],
                 f"clone={snap['clone']} dl={snap['dl']} tx={snap['tx']} quote={snap['quote']}")
        ck.check("倍速档位 ×5", snap["rates"] == 5, f"rates={snap['rates']}")
        page.evaluate("() => { const b=[...document.querySelectorAll("
                      "'#voice-more-menu .vm-rate')].find(x=>x.getAttribute('data-r')==='1.5');"
                      " if (b) b.click(); }")
        page.wait_for_timeout(300)
        rt = page.evaluate(
            "() => { const as_=[...document.querySelectorAll('audio.msg-media-audio')];"
            " return { all: as_.length>0 && as_.every(a=>Math.abs(a.playbackRate-1.5)<0.01),"
            " saved: localStorage.getItem('ws_voice_rate_v1')==='1.5' }; }")
        ck.check("点 1.5x → 全部语音生效 + 偏好落 localStorage",
                 rt["all"] and rt["saved"], f"all={rt['all']} saved={rt['saved']}")
        # P1：播放器上的倍速 chip 文本随全局倍速同步（_vmSetRate 刷新 .vp-rate）
        ck.check("播放器倍速 chip 同步为 1.5x",
                 page.evaluate("() => { const r=document.querySelector('#msg-area .vp .vp-rate');"
                               " return !!r && r.textContent.trim()==='1.5x'; }"))
        page.evaluate("() => { const b=[...document.querySelectorAll("
                      "'#voice-more-menu .vm-rate')].find(x=>x.getAttribute('data-r')==='1');"
                      " if (b) b.click(); }")  # 复位 1x，避免污染后续/坐席

        print("== 4. 克隆弹窗：表单就绪 + 授权闸门 + 可关闭 ==")
        page.evaluate("() => document.querySelector("
                      "'#voice-more-menu [data-act=clone]').click()")
        # 表单要等人设清单接口返回；轮询到 #vcm-persona 出现（上限 15s）
        try:
            page.wait_for_function("() => !!document.getElementById('vcm-persona')",
                                   timeout=15000)
        except Exception:
            pass
        form = page.evaluate(
            "() => { const ov=document.getElementById('vcm-overlay');"
            " const nm=document.getElementById('vcm-name');"
            " const ps=document.getElementById('vcm-persona');"
            " const cs=document.getElementById('vcm-consent');"
            " return { shown:ov.classList.contains('show'),"
            " name:nm?String(nm.value||''):null,"
            " personas:ps?ps.options.length:0,"
            " consent:cs?cs.checked:null,"
            " submit:!!ov.querySelector('[data-act=vcm-submit]'),"
            " audio:!!ov.querySelector('audio') }; }")
        ck.check("弹窗打开且表单就绪",
                 form["shown"] and form["submit"] and form["audio"],
                 f"shown={form['shown']} submit={form['submit']} audio={form['audio']}")
        ck.check("音色名已预填（联系人/说话人名）", bool(form["name"]),
                 f"name={form['name']!r}")
        ck.check("人设下拉有可选项", (form["personas"] or 0) > 1,
                 f"options={form['personas']}")
        ck.check("授权默认未勾选（必须人工确认）", form["consent"] is False)
        # 授权闸门：未勾选点提交 → 前端拦下（不发请求）。同时监听网络确证零请求。
        hits = []
        page.on("request", lambda req: hits.append(req.url)
                if "/api/voice/enroll" in req.url else None)
        page.evaluate("() => document.querySelector("
                      "'#vcm-overlay [data-act=vcm-submit]').click()")
        page.wait_for_timeout(600)
        gate = page.evaluate(
            "() => { const h=document.getElementById('vcm-hint');"
            " return { err:!!h && h.className.indexOf('err')>=0,"
            " txt:h?(h.textContent||'').trim():'' }; }")
        ck.check("授权未勾选 → 前端闸门拦下并提示",
                 gate["err"] and bool(gate["txt"]), f"hint={gate['txt'][:24]!r}")
        ck.check("闸门期零 enroll 请求", not hits, f"requests={len(hits)}")
        page.evaluate("() => document.querySelector("
                      "'#vcm-overlay [data-act=vcm-close]').click()")
        page.wait_for_timeout(300)
        ck.check("弹窗可关闭",
                 page.evaluate("() => !document.getElementById('vcm-overlay')"
                               ".classList.contains('show')"))
        browser.close()
    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="语音克隆 UI 真浏览器门禁（只读）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)

    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装")
        return 0
    token = read_token(args.data_root)
    if not token:
        print("[SKIP] 读不到 auth_token（数据根不对？）")
        return 0
    try:
        import urllib.request
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception:
        print("[SKIP] 实例不可达: " + args.base)
        return 0
    try:
        return run(args.base, token, data_root=args.data_root, headed=args.headed)
    except Exception as ex:  # noqa: BLE001
        print(f"[FAIL] 执行异常: {type(ex).__name__}: {ex}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
