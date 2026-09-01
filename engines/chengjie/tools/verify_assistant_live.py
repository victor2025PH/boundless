# -*- coding: utf-8 -*-
"""小智面板 **真实例** 端到端点击验证（2026-08-28，老板要求「你来点」）。

与同族 `verify_assistant_ui.py` 的分工——那个跑在本地 fixture + stub fetch 上，
能证明「前端逻辑自洽」，**证不了**「坐席在真站点上点得通」：真站点多了登录态、
RBAC、多窗口协调器、真检索语料、真 LLM 流、7 层中间件与压缩层。2026-08-28 那起
「网络异常」事故正是全栈组合才暴露的（fixture 一路全绿）。

只读边界（**改动前先读这段**）：
  · 会真提问 → 消耗一次 LLM 调用并在 qa_log 落一行（那本就是问答账本）；
  · **绝不**点「替我做」的执行/示例 chip —— runGoal 会真改配置；
  · **绝不**提交报障 —— 会写 bug_tickets 并触发告警；
  · **绝不**发消息、不碰任何客户会话。
UI 结构一律只断言、不驱动写操作。

用法::

    python tools/verify_assistant_live.py                  # 门禁模式
    python tools/verify_assistant_live.py --shots out/     # 存截图
    python tools/verify_assistant_live.py --headed         # 肉眼看一遍

环境缺失（无 playwright / 实例不可达）一律 SKIP exit 0，不污染回归信号。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

DEFAULT_BASE = "http://127.0.0.1:18799"
DEFAULT_DATA_ROOT = r"D:\chengjie-instances\zhiliao\data"
# 桌面壳侧栏实测宽度（老板截图的形态）。面板 CSS 在 <=520px 走窄屏档。
VIEWPORT = {"width": 480, "height": 900}


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: List[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))
        return bool(ok)

    def summary(self) -> int:
        total = self.passed + len(self.failed)
        print(f"\n{self.passed}/{total} passed")
        if self.failed:
            print("failed:")
            for f in self.failed:
                print(f"  - {f}")
            return 1
        return 0


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；绝不打印）。"""
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


def _shot(page, shots: Optional[Path], name: str) -> None:
    if shots is None:
        return
    try:
        page.screenshot(path=str(shots / f"{name}.png"))
    except Exception:
        pass


def run(page, ck: Checker, shots: Optional[Path]) -> None:
    ev = page.evaluate

    # ── 0. 球在场（assistant.enabled + 组件加载）────────────────────────────
    page.wait_for_selector(".asb-ball", timeout=20000)
    ck.check("L0 小智球在真工作台渲染", ev("() => !!document.querySelector('.asb-ball')"))

    # 多窗口协调器：若别处已有活跃坐席，本窗会被推到待机（轮询停、SSE 断）。
    # 那不是缺陷，但会让后续断言测的是待机态 → 显式认领主控再继续。
    ev("() => { try { var b = document.querySelector("
       "'[data-act=\"mw-takeover\"], .ws-mw-take'); if (b) b.click(); } "
       "catch (e) {} }")
    page.wait_for_timeout(300)

    page.click(".asb-ball")
    page.wait_for_timeout(700)
    ck.check("L1 点球开面板", ev("() => !!document.querySelector('.asb-panel.open')"))
    _shot(page, shots, "01_panel_open")

    # ── 1. 首屏引导（老板原话「没有引导指示」）─────────────────────────────
    # 8/23-8/28 这里显示的是裸键名 "hello"（t() 缺键回落）。裸键的判据不能只看
    # 「有没有 hello 这个词」，得看**首屏第一句是不是仍等于键名本身**。
    first = ev("() => { var m = document.querySelector('.asb-body .asb-msg.ai');"
               " return m ? String(m.textContent || '').trim() : ''; }")
    ck.check("L2 首屏第一句不是裸键名（hello 缺键已修）",
             bool(first) and first.strip().lower() != "hello", f"first={first[:40]!r}")
    ck.check("L2b 首屏一句话讲清三件事（查用法/带你操作/替你动手）",
             all(k in first for k in ("查用法", "带你操作", "替你动手")),
             f"first={first[:60]!r}")
    ck.check("L2c 首屏给了可点的本页常问 chip",
             ev("() => document.querySelectorAll('.asb-chip').length >= 1"),
             ev("() => document.querySelectorAll('.asb-chip').length + ' chips'"))

    # ── 2. 真提问：走真检索 + 真 LLM 流（截图 2/3 的「网络异常」）───────────
    # 这是本工具存在的理由：fixture 里 stub 一路全绿，而真链路曾 100% 断在
    # meta 之后 0.4s。不做超时 abort——服务端刻意无秒数上限，等就是了。
    ev("() => { var i = document.querySelector('.asb-in'); if (i) { "
       "i.value = '怎么发语音'; i.dispatchEvent(new Event('input', "
       "{bubbles:true})); } }")
    page.click(".asb-send")
    # 必须等**流真正收尾**，不能一见到字就判：打字机进行中 last.textContent
    # 已经很长，但 sources 是 done 之后才补渲染的（首版断言就栽在这，L3b 假红）。
    # 收尾判据＝发送键从 ⏹(stop-gen) 复位，与 setBusy(false) 同一时刻。
    for _ in range(75):  # 最多等 75s；服务端刻意无秒数上限，等就是了
        page.wait_for_timeout(1000)
        if not ev("() => !!document.querySelector("
                  "'.asb-send[data-act=\"stop-gen\"]')"):
            break
        if ev("() => !!document.querySelector('.asb-body .asb-msg.err')"):
            break
    page.wait_for_timeout(400)
    got = ev("() => { var ms = document.querySelectorAll("
             "'.asb-body .asb-msg.ai'); if (!ms.length) return false; "
             "var last = ms[ms.length-1]; var tx = String(last.textContent"
             " || ''); return tx.length > 12 && tx.indexOf('检索帮助库') "
             "< 0 && tx.indexOf('思考中') < 0 && tx.indexOf('正在问 AI')"
             " < 0; }")
    _shot(page, shots, "02_answered")
    err_txt = ev("() => { var e = document.querySelector('.asb-body .asb-msg.err');"
                 " return e ? String(e.textContent || '').trim() : ''; }")
    ck.check("L3 真提问拿到真回答（不再是「网络异常」）", got and not err_txt,
             f"err={err_txt[:60]!r}")
    ck.check("L3b 回答带来源引用（检索命中而非裸编）",
             ev("() => !!document.querySelector('.asb-srcs')"))

    # ── 2b. 拒答不是死路（2026-08-29 老板实录「回复的内容没一点帮助」）───────
    # 用一个**真的会走 NO_BASIS 哨兵**的问句：检索命中 3 条且都过 min_score，
    # 但答题 LLM 判定它们回答不了 → answered=false。旧行为会把那 3 条无关来源
    # 照旧列出来，读成「它找到了却不肯说」。零命中问句测不到这一幕（没 sources）。
    ev("() => { var i = document.querySelector('.asb-in'); if (i) { "
       "i.value = '怎么导出所有客户的手机号'; i.dispatchEvent(new Event("
       "'input', {bubbles:true})); } }")
    page.click(".asb-send")
    for _ in range(75):
        page.wait_for_timeout(1000)
        if not ev("() => !!document.querySelector("
                  "'.asb-send[data-act=\"stop-gen\"]')"):
            break
    page.wait_for_timeout(500)
    _shot(page, shots, "02b_refusal")
    refused = ev("() => { var ms = document.querySelectorAll("
                 "'.asb-body .asb-msg.ai'); if (!ms.length) return false; "
                 "return String(ms[ms.length-1].textContent||'')"
                 ".indexOf('没有找到可靠依据') > -1; }")
    if refused:
        ck.check("L3c 拒答不列来源（那些条目正是被判定答不了的）",
                 ev("() => { var b = document.querySelector('.asb-body'); "
                    "var k = b ? b.children : []; var lastAi = -1; "
                    "for (var i = 0; i < k.length; i++) { if (k[i].classList"
                    ".contains('asb-msg') && k[i].classList.contains('ai')) "
                    "{ lastAi = i; } } for (var j = lastAi + 1; j < k.length;"
                    " j++) { if (k[j].classList.contains('asb-srcs')) "
                    "{ return false; } } return true; }"))
        ck.check("L3d 拒答给出「我答得上」的建议（死路变菜单）",
                 ev("() => document.querySelectorAll("
                    "'.asb-sugg .asb-chip[data-q][data-sugg]').length >= 1"),
                 ev("() => document.querySelectorAll('.asb-sugg .asb-chip')"
                    ".length + ' sugg'"))
        ck.check("L3e 拒答仍保留报障出路",
                 ev("() => !!document.querySelector('[data-act=\"to-report\"]')"))
    else:
        # 语料补齐后这句可能变成答得上——那是好事，但本段就失去了被测对象。
        # 如实报告而不是假绿：换一个仍会走哨兵的问句进来。
        print("  SKIP  L3c-L3e 拒答路径（该问句现在答得上了，需换探测问句）")

    # ── 3.「替我做」模式：截图 1「版面太碎、没有重点」的三项修复 ────────────
    modes = ev("() => document.querySelectorAll('.asb-mode').length")
    ck.check("L4 模式条三格在场（问答/教学/替我做都认领成功）", modes == 3,
             f"{modes} modes")
    if ev("() => !!document.querySelector('.asb-mode[data-mode=\"agent\"]')"):
        page.click('.asb-mode[data-mode="agent"]')
        page.wait_for_timeout(800)
        _shot(page, shots, "03_agent_mode")
        ck.check("L5 整屏只有一颗实心主按钮，且长在主卡上",
                 ev("() => document.querySelectorAll('.asb-md .asb-md-go')"
                    ".length === 1 && !!document.querySelector("
                    "'.asb-md-hero--pri .asb-md-go')"),
                 ev("() => document.querySelectorAll('.asb-md .asb-md-go')"
                    ".length + ' go'"))
        ck.check("L5b 手机指挥已降级为一行次要入口（通道≠能力）",
                 ev("() => !!document.querySelector("
                    "'.asb-md-sub[data-xza=\"mode-pair\"]')"))
        ck.check("L5c 最近做过：标题行自带「全部 →」文字链",
                 ev("() => !!document.querySelector("
                    "'.asb-md-hd .asb-md-lnk[data-xza=\"mode-hist\"]')"))
        # 主按钮只把光标送进输入框（不提交）→ 点它是安全的
        ev("() => { var i = document.querySelector('.asb-in'); if (i) i.blur(); }")
        page.click('.asb-md-go[data-xza="mode-start"]')
        page.wait_for_timeout(400)
        ck.check("L5d 点主按钮 → 光标进输入框（说明→输入的衔接）",
                 ev("() => document.activeElement === "
                    "document.querySelector('.asb-in')"),
                 ev("() => (document.activeElement && "
                    "(document.activeElement.className || "
                    "document.activeElement.tagName)) || 'none'"))
        # 首屏密度：碎不碎是可量的——同级区块数 + 固定件占比
        blocks = ev("() => document.querySelectorAll('.asb-md > *').length")
        ck.check("L5e 模式区同级区块 <= 4（原本 5 块塞满 398px 内容区）",
                 isinstance(blocks, int) and blocks <= 4, f"{blocks} blocks")

    # ── 4. 教学模式：进入的理由必须是一句真话 ──────────────────────────────
    if ev("() => !!document.querySelector('.asb-mode[data-mode=\"teach\"]')"):
        page.click('.asb-mode[data-mode="teach"]')
        page.wait_for_timeout(1200)
        _shot(page, shots, "04_teach_mode")
        stat = ev("() => { var s = document.querySelector('.asb-md-stat'); "
                  "return s ? String(s.textContent || '').trim() : ''; }")
        ck.check("L6 教学模式给出「本页可讲解处」状态行（三态诚实，非空壳）",
                 bool(stat), f"stat={stat[:50]!r}")
        ck.check("L6b 状态行不是裸键名",
                 not stat.startswith("teach_") and "_" not in stat.split(" ")[0],
                 f"stat={stat[:50]!r}")

    # ── 5. 次级页签只读巡检（绝不提交）─────────────────────────────────────
    for tab, label in (("faq", "常问"), ("mine", "我的")):
        if ev(f"() => !!document.querySelector('.asb-sub[data-tab=\"{tab}\"]')"):
            page.click(f'.asb-sub[data-tab="{tab}"]')
            page.wait_for_timeout(1200)
            _shot(page, shots, f"05_{tab}")
            body = ev("() => { var b = document.querySelector('.asb-body'); "
                      "return b ? String(b.textContent || '').trim() : ''; }")
            ck.check(f"L7 「{label}」页签有内容（非空白、非裸键、非红错）",
                     len(body) > 4 and "网络异常" not in body,
                     f"body={body[:50]!r}")

    # ── 6. 全程零未捕获 JS 异常（哑按钮/裸键的运行时兜底信号）──────────────
    _shot(page, shots, "06_final")


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="小智面板真实例点击验证（只读；不执行动作、不提交工单）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--token", default="")
    ap.add_argument("--shots", default="")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)

    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
    except Exception:
        print("[SKIP] 未装 playwright")
        return 0

    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except urllib.error.HTTPError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达（{args.base}）：{str(e)[:80]}")
        return 0

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[ABORT] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 2

    shots = None
    if args.shots:
        shots = Path(args.shots)
        shots.mkdir(parents=True, exist_ok=True)

    ck = Checker()
    errors: List[str] = []
    print(f"== 真实例点击验证 {args.base}（视口 "
          f"{VIEWPORT['width']}x{VIEWPORT['height']}）==")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(viewport=VIEWPORT)
        ctx.request.post(args.base + "/login", form={"auth_token": token})
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(args.base + "/workspace", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        try:
            run(page, ck, shots)
        finally:
            ck.check("L9 全程零未捕获 JS 异常", not errors, "; ".join(errors[:3]))
            browser.close()
    return ck.summary()


if __name__ == "__main__":
    raise SystemExit(main())
