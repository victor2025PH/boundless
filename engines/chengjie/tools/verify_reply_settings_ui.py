# -*- coding: utf-8 -*-
"""「自动回复设置」页（/reply-settings）可读性/布局真浏览器门禁（Playwright；2026-08-02 沉淀）。

**为什么需要它**：/reply-settings 刚完成一轮可读性/布局修复，而这类退化的共同
形态是**不报错、不变红、截图乍看也正常**——全局 CSS 改一行就可能悄悄回潮，
肉眼 review 根本看不出来。每条断言都对应一个真实踩过的历史 bug：

覆盖的不变量（与代码内 1..8 编号一致）：
  1. **inline 盒回归钉**：`.rps-mode-box` 曾是 inline `<span>`，行内盒随文本流
     断行 → 边框被拆成多段碎片；修复后 computed display 必须是 `flex`。
  2. **输入框宽度钉**：`base.html` 全局 `input{width:100%}` 曾把 `#rps-minlen`
     （最小字数的小数字框）拉满整行；clientWidth 必须落在 60~240px 合理带。
  3. **可读性钉**：`.rps-hint` 说明文字曾用 `--t3`（rgb(133, 136, 143)）+ 小
     字号，深底浅灰小字实际不可读；现应为 `--t2` 且 fontSize >= 12.4px。
  4. **色带选区钉**：`#rps-zone-sel`（时段色带选区控件）必须存在。
  5. **链接色钉**：`a.rps-gate-link` 曾未钉颜色，被浏览器 `:visited` 默认紫红
     污染；现应为品牌蓝 —— computed color 的 blue 通道 > red 通道。
  6. **预览播放钉**（P1 新功能）：`#rps-pv-play` 按钮存在；点击后 500ms 内
     `disabled`（播放中），20s 内恢复 enabled。纯前端动画，无任何写操作。
  7. **高级折叠组钉**：`details.rps-adv` 存在且默认收起（open=false）；
     `#rps-voice` 在折叠组内仍在 DOM（details 收起只是视觉隐藏，
     getElementById 必须命中）。
  8. **焦点环钉**（弱断言）：第一个 `.rps-mode input` 执行 focus() 后成为
     document.activeElement（可聚焦即可，不强验 outline 渲染）。

P2 增补（2026-08-02 同日；中文主视图追加 9..11，另开英文视图 12..15；
汇总改两段式「中文视图 N/N + 英文视图 M/M」，总 PASS = 两段全绿）：
  9.  **拟人度评分钉**：`#rps-score` 存在、className 含 rps-score 且含
      ok/mid/bad 档位之一、文案非空（守评分徽章接线）。
  10. **实测均值容器钉**：`#rps-zone-obs` 存在于 DOM 即可——无实测数据时
      display:none 是合法态，刻意不钉可见性（守实测上轴容器）。
  11. **锚点联动钉**：`.rps-anchors` 存在且 a 链接 >= 4（2026-08-29 收成四组
      sticky：接管 / 额度 / 节奏 / 平台）；滚到 `#rps-adv`
      后 IntersectionObserver 至少点亮 1 个 a.active（守 IO 滚动联动；
      IO 回调异步，自带「多等一拍重试一次」防 flaky）。
  12. **EN 播放按钮钉**：lang=en 下 `#rps-pv-play` 文案含 "Play preview"
      （守 EN 词条完整性；lang 查询参数强制英文渲染，query 优先于 cookie）。
  13. **EN 静态标签零 CJK 钉**：`.rps-wrap` 下 h3 / .rps-row-label /
      .rps-anchors a / summary 文案零 CJK——只扫静态标签元素、不扫整页，
      人设名等数据驱动内容含中文是合法的（守 EN 词条完整性）。
  14. **EN 布局钉**：首个 `.rps-row-label` clientWidth <= 240（英文长标签
      在 230px 标签列内换行而非撑破网格；守 EN 网格不破版）。
  15. **EN 全页截图**：reply_settings_verify_en.png 独立留档（诊断产物）。

P0-caps 增补（2026-08-03；中文视图追加 16..17）：
  16. **改动明细抽屉钉**：翻一个开关 → 保存条出现 → 点「改动明细」展开
      >= 1 行（老值 → 新值）→ rpsReset 还原 → 保存条消失。纯前端往返，
      全程不点保存，零写操作（与 6 的预览播放同性质）。
  17. **能力清单/封顶容器钉**：`#rps-cap-note`（档位封顶提示）与
      `#rps-caps-markread` / `#rps-caps-typing`（已读/打字平台能力清单）
      存在于 DOM。display:none 是合法态（platform_modes 未配置 / 旧进程
      未回 platform_caps 字段），可见性由数据决定，刻意不钉。

P2 额度组（2026-08-29）：中文视图追加 18。**不点全自动 / 不点保存**。
  18. **额度组 / 发送闸门卡 / 快捷档 / sticky 锚点**：`#rps-grp-quota` 在 DOM；
      `#rps-sec-sendgate` 可见；feat 装载后 `#rps-sg-body` 可见；出厂
      `rpsGuardQuick(500)` 按钮在 DOM；`.rps-anchors` 为 sticky；中文
      守卫 h3 含「单会话额度」、发送闸门 h3 含「单账号日发」；危险确认框
      `#rps-confirm` 在 DOM（本工具不打开它）。

用法::

    python tools/verify_reply_settings_ui.py             # 门禁模式（默认截图到 out/）
    python tools/verify_reply_settings_ui.py --headed    # 肉眼看一遍
    python tools/verify_reply_settings_ui.py --shots o2  # 换截图目录（留空不截图）

**只读性质**：不写任何配置、不发任何消息。全部断言都是 getComputedStyle /
clientWidth / DOM 存在性读取；仅有的「交互」是预览播放按钮（纯前端波形
动画）、focus() 与 scrollIntoView()（锚点联动钉），均不落盘、不出站。
与 ``tools/verify_inbox_density.py`` /
``verify_account_rail_ui.py`` 同族（同一实例 + token 登录 + Playwright +
SKIP exit 0 语义）。

token 从实例数据根读取，**绝不打印**。缺 playwright / 读不到 token / 实例
不可达 → 打印 [SKIP] 原因并 exit 0（挂回归清单的前提：环境缺失不污染回归
信号）；任一断言失败 exit 1；全过 exit 0 并打印 [PASS] 摘要。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：Windows 下先试 ::1 而服务只听 IPv4，每个新连接吃 ~2s 回退超时（verify_inbox_density 实测 16ms vs 2070ms）
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

# 目标页深链：带会话上下文（平台/账号/会话）页面才按「某个具体会话」渲染全量
# 控件（档位盒/语音/门控/预览区块），断言面完整。
PAGE_PATH = "/reply-settings?ctx_platform=telegram&ctx_account=default&ctx_chat=5433982810"

# 量测视口固定，否则宽度断言不可比（换宽度会改变栅格换行）；@2x 让截图可放大
# 细看边框碎裂/小字渲染这类像素级问题。
VIEWPORT = {"width": 1440, "height": 1000}
DEVICE_SCALE = 2

# --t3 旧值：.rps-hint 曾用它 + 小字号 → 深底浅灰小字不可读（现应为 --t2）。
# 比对用去空格归一化形态（Chromium 返回 "rgb(133, 136, 143)" 带空格）。
_OLD_T3_COLOR = "rgb(133,136,143)"


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。

    与同族 verify_*_ui.py 各自持有一份（工具保持自包含、可单独拷走运行）。
    """
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


# 两段式汇总的段落标签（P2 起）：总 PASS 判定 = 两段全绿（任一 FAIL 即 exit 1）。
SEG_ZH = "中文视图"
SEG_EN = "英文视图"


class Checker:
    def __init__(self) -> None:
        # (名称, 通过?, 段落)——段落只服务两段式汇总，check() 调用侧无感知。
        self.results: List[Tuple[str, bool, str]] = []
        self.skipped: List[str] = []
        self.segment = SEG_ZH

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok, self.segment))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  [SKIP] {name}  {why}")

    def summary(self) -> int:
        fails = [n for n, ok, _seg in self.results if not ok]
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        # 两段式读数：「中文视图 N/N + 英文视图 M/M」。某段一项都没跑到（如
        # 中文段早退）就不显示该段，避免 0/0 干扰；PASS/FAIL 判定仍看全部结果。
        parts: List[str] = []
        for seg in (SEG_ZH, SEG_EN):
            rows = [ok for _n, ok, s in self.results if s == seg]
            if rows:
                parts.append(f"{seg} {sum(1 for ok in rows if ok)}/{len(rows)}")
        line = " + ".join(parts) if parts else "0/0"
        if fails:
            print(f"\n== [FAIL] 自动回复设置页验收: {line}{extra}  FAILED: {fails}")
            return 1
        print(f"\n== [PASS] 自动回复设置页验收: {line}{extra}")
        return 0


def _parse_rgb(color: str) -> Optional[Tuple[float, float, float]]:
    """'rgb(91, 140, 255)' / 'rgba(91,140,255,.9)' → (91.0, 140.0, 255.0)；解析不了返 None。"""
    if not color:
        return None
    nums = re.findall(r"[\d.]+", color)
    if len(nums) < 3:
        return None
    try:
        return float(nums[0]), float(nums[1]), float(nums[2])
    except ValueError:
        return None


def run(base: str, token: str, *, shots: Optional[Path] = None,
        headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=DEVICE_SCALE)
        ctx.request.post(base + "/login", form={"auth_token": token})
        # 对齐完整模式：简洁模式下部分高级控件不渲染，断言口径必须钉死在 full
        ctx.request.get(base + "/set_ui_mode?mode=full")
        page = ctx.new_page()
        page.goto(base + PAGE_PATH, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(".rps-mode-box", timeout=20000)
        except Exception:
            # 页面按「最终形态」验收：档位盒都没有＝改造未落地或实例在重启，
            # 这属于门禁该点名的形态（任务规格明示改造完成后才挂回归）。
            ck.check("0. 页面就绪（.rps-mode-box 出现）", False,
                     "20s 超时——页面改造未落地或实例在重启")
            try:
                if shots:
                    page.screenshot(path=str(shots / "reply_settings_verify.png"),
                                    full_page=True)
            except Exception:
                pass
            browser.close()
            return ck.summary()
        page.wait_for_timeout(1500)  # 等异步渲染（人设列表/预览波形等）就位

        # ── 1. inline 盒回归钉 ────────────────────────────────────────────
        # 历史 bug：.rps-mode-box 曾是 inline <span>，行内盒随文本流断行，
        # 边框被拆成多段碎片（每行各画一圈）。修复后必须是 flex 盒。
        print("== 1. inline 盒回归钉（.rps-mode-box）==")
        disp = page.eval_on_selector(".rps-mode-box",
                                     "el => getComputedStyle(el).display")
        ck.check("1. .rps-mode-box 的 computed display 为 flex",
                 disp == "flex", f"display={disp}")

        # ── 2. 输入框宽度钉 ──────────────────────────────────────────────
        # 历史 bug：base.html 全局 input{width:100%} 泄漏进本页，把最小字数
        # 小数字框拉满整行。60~240px 合理带：<60=塌缩，>240=全局宽度回潮。
        print("== 2. 输入框宽度钉（#rps-minlen）==")
        w = page.evaluate("() => { const el = document.getElementById('rps-minlen');"
                          " return el ? el.clientWidth : null; }")
        ck.check("2. #rps-minlen 宽度落在 60~240px",
                 w is not None and 60 <= w <= 240,
                 f"clientWidth={w}px")

        # ── 3. 可读性钉 ──────────────────────────────────────────────────
        # 历史 bug：.rps-hint 用 --t3（rgb(133,136,143)）+ 小字号，深底浅灰
        # 小字实际不可读。现应为 --t2 且字号 >= 12.4px。
        print("== 3. 可读性钉（.rps-hint 字号/颜色）==")
        hint = page.evaluate(
            "() => { const el = document.querySelector('.rps-hint');"
            " if (!el) return null; const cs = getComputedStyle(el);"
            " return {fs: parseFloat(cs.fontSize), color: cs.color}; }")
        if hint is None:
            ck.check("3. 首个 .rps-hint 存在", False, "未找到 .rps-hint")
        else:
            ck.check("3a .rps-hint 字号 >= 12.4px",
                     hint["fs"] >= 12.4, f"fontSize={hint['fs']}px")
            col_norm = (hint["color"] or "").replace(" ", "")
            ck.check("3b .rps-hint 颜色已离开旧 --t3（rgb(133,136,143)）",
                     col_norm != _OLD_T3_COLOR, f"color={hint['color']}")

        # ── 4. 色带选区钉 ────────────────────────────────────────────────
        print("== 4. 色带选区钉（#rps-zone-sel）==")
        ck.check("4. #rps-zone-sel 存在",
                 page.evaluate("() => !!document.getElementById('rps-zone-sel')"))

        # ── 5. 链接色钉 ──────────────────────────────────────────────────
        # 历史 bug：a.rps-gate-link 未钉颜色，被浏览器 :visited 默认紫红污染
        # （点过一次后变紫红，与品牌蓝主题割裂）。品牌蓝判据＝blue 通道 > red。
        print("== 5. 链接色钉（a.rps-gate-link）==")
        lc = page.evaluate("() => { const el = document.querySelector('a.rps-gate-link');"
                           " return el ? getComputedStyle(el).color : null; }")
        rgb = _parse_rgb(lc or "")
        ck.check("5. 门控链接为品牌蓝（blue 通道 > red 通道）",
                 rgb is not None and rgb[2] > rgb[0], f"color={lc}")

        # ── 6. 预览播放钉（P1 新功能；纯前端动画，无写操作）─────────────
        print("== 6. 预览播放钉（#rps-pv-play）==")
        has_play = page.evaluate("() => !!document.getElementById('rps-pv-play')")
        ck.check("6a #rps-pv-play 按钮存在", has_play)
        if not has_play:
            ck.skip("6b/6c 播放态往返", "按钮缺失，无从点击")
        else:
            clicked = True
            try:
                page.click("#rps-pv-play", timeout=5000)
            except Exception as e:  # noqa: BLE001  被遮挡/不可点也算 6b 失败
                clicked = False
                ck.check("6b 点击后 500ms 内进入播放态（disabled）", False,
                         f"click 失败: {str(e)[:60]}")
            if clicked:
                try:
                    page.wait_for_function(
                        "() => { const b = document.getElementById('rps-pv-play');"
                        " return !!b && b.disabled === true; }", timeout=500)
                    ck.check("6b 点击后 500ms 内进入播放态（disabled）", True)
                except Exception:
                    ck.check("6b 点击后 500ms 内进入播放态（disabled）", False,
                             "500ms 内未见 disabled=true")
                try:
                    page.wait_for_function(
                        "() => { const b = document.getElementById('rps-pv-play');"
                        " return !!b && b.disabled === false; }", timeout=20000)
                    ck.check("6c 20s 内恢复可点（enabled）", True)
                except Exception:
                    ck.check("6c 20s 内恢复可点（enabled）", False,
                             "20s 超时仍 disabled（动画未收尾？）")

        # ── 7. 高级折叠组钉 ──────────────────────────────────────────────
        # details 收起只是视觉隐藏，DOM 仍在——#rps-voice 必须能 getElementById
        # 命中（页面脚本按 id 初始化，折叠不该断掉初始化链）。
        print("== 7. 高级折叠组钉（details.rps-adv / #rps-voice）==")
        adv = page.evaluate(
            "() => { const d = document.querySelector('details.rps-adv');"
            " return {found: !!d, open: d ? d.open : null,"
            "         voice: !!document.getElementById('rps-voice')}; }")
        ck.check("7a details.rps-adv 存在且默认收起",
                 adv["found"] and adv["open"] is False,
                 f"found={adv['found']} open={adv['open']}")
        ck.check("7b #rps-voice 在 DOM（折叠组内仍可寻址）", adv["voice"])

        # ── 8. 焦点环钉（弱断言：可聚焦即可，不强验 outline 渲染）────────
        print("== 8. 焦点环钉（.rps-mode input 可聚焦）==")
        foc = page.evaluate(
            "() => { const inp = document.querySelector('.rps-mode input');"
            " if (!inp) return {found: false, focused: false};"
            " inp.focus();"
            " return {found: true, focused: document.activeElement === inp}; }")
        ck.check("8. 首个 .rps-mode input focus() 后成为 activeElement",
                 foc["found"] and foc["focused"],
                 f"found={foc['found']} focused={foc['focused']}")

        # ── 9. 拟人度评分钉（P2）：守评分徽章接线——徽章在、ok/mid/bad 档位
        #      class 在、文案非空（缺一＝评分脚本没跑或数据断供）─────────────
        print("== 9. 拟人度评分钉（#rps-score）==")
        sc = page.evaluate(
            "() => { const el = document.getElementById('rps-score');"
            " if (!el) return null;"
            " return {cls: el.getAttribute('class') || '',"
            "         text: (el.textContent || '').trim()}; }")
        if sc is None:
            ck.check("9. #rps-score 存在且档位 class/文案就位", False,
                     "未找到 #rps-score")
        else:
            ck.check("9. #rps-score class 含 rps-score + ok/mid/bad 之一、文案非空",
                     ("rps-score" in sc["cls"])
                     and any(t in sc["cls"] for t in ("ok", "mid", "bad"))
                     and bool(sc["text"]),
                     f"class={sc['cls']!r} text={sc['text'][:24]!r}")

        # ── 10. 实测均值容器钉（P2）：守实测上轴容器——只钉 DOM 存在性；
        #       display:none（该会话暂无实测数据）是合法态，刻意不钉可见性 ──
        print("== 10. 实测均值容器钉（#rps-zone-obs）==")
        ck.check("10. #rps-zone-obs 存在于 DOM（display 可为 none）",
                 page.evaluate("() => !!document.getElementById('rps-zone-obs')"))

        # ── 11. 锚点联动钉（P2）：守 IO 滚动联动——锚点导航齐全 + 滚动后
        #       IntersectionObserver 点亮当前区块锚点 ────────────────────────
        print("== 11. 锚点联动钉（.rps-anchors × IntersectionObserver）==")
        anch = page.evaluate(
            "() => { const w = document.querySelector('.rps-anchors');"
            " const as = w ? Array.from(w.querySelectorAll('a')) : [];"
            " return {found: !!w,"
            "         links: as.length,"
            "         hrefs: as.map(a => a.getAttribute('href') || '')}; }")
        ck.check("11a .rps-anchors 存在且 a 链接数 >= 4（含 #rps-grp-quota）",
                 anch["found"] and anch["links"] >= 4
                 and "#rps-grp-quota" in anch["hrefs"],
                 f"found={anch['found']} links={anch['links']}"
                 f" hrefs={anch.get('hrefs')}")
        # IO 回调是异步的，时机受渲染/布局抖动影响 → 防 flaky：滚动后等 800ms
        # 判一次，未命中则补滚一次并多等 1200ms 重试，仍无 active 才判 FAIL。
        if not page.evaluate("() => !!document.getElementById('rps-adv')"):
            ck.check("11b 滚动到 #rps-adv 后 .rps-anchors a.active >= 1", False,
                     "缺滚动目标 #rps-adv")
        else:
            active_n = 0
            for wait_ms in (800, 1200):
                page.evaluate(
                    "() => document.getElementById('rps-adv').scrollIntoView()")
                page.wait_for_timeout(wait_ms)
                active_n = page.evaluate(
                    "() => document.querySelectorAll('.rps-anchors a.active').length")
                if active_n >= 1:
                    break
            ck.check("11b 滚动到 #rps-adv 后 .rps-anchors a.active >= 1",
                     active_n >= 1, f"active={active_n}")

        # ── 16. 改动明细抽屉钉（P0-caps 批次）：纯前端往返——翻一个开关 →
        #       保存条出现 → 「改动明细」展开有行 → 还原 → 保存条消失。
        #       全程不点保存，零写操作（与 6 的预览播放同性质）。────────────
        print("== 16. 改动明细抽屉钉（#rps-diff-btn / #rps-diff-panel）==")
        has_diff = page.evaluate(
            "() => !!document.getElementById('rps-diff-btn')"
            " && !!document.getElementById('rps-diff-panel')")
        ck.check("16a 明细按钮与面板存在", has_diff)
        if not has_diff:
            ck.skip("16b/16c/16d 抽屉往返", "按钮/面板缺失，无从点击")
        else:
            page.evaluate("() => document.getElementById('rps-adaptive').click()")
            shown = page.evaluate(
                "() => (document.getElementById('rps-savebar').className || '')"
                ".indexOf('show') >= 0")
            ck.check("16b 翻开关后保存条出现", shown)
            try:
                page.click("#rps-diff-btn", timeout=5000)
                rows = page.evaluate(
                    "() => { const p = document.getElementById('rps-diff-panel');"
                    " if (!p || p.style.display === 'none') return -1;"
                    " return p.querySelectorAll('.rps-diff-row').length; }")
                ck.check("16c 明细面板展开且 >= 1 行（老值 → 新值）",
                         rows >= 1, f"rows={rows}")
            except Exception as e:  # noqa: BLE001  被遮挡/不可点也算失败
                ck.check("16c 明细面板展开且 >= 1 行（老值 → 新值）", False,
                         f"click 失败: {str(e)[:60]}")
            page.evaluate("() => window.rpsReset()")
            hidden = page.evaluate(
                "() => (document.getElementById('rps-savebar').className || '')"
                ".indexOf('show') < 0")
            ck.check("16d 还原后保存条消失（表单回基线）", hidden)

        # ── 17. 能力清单/封顶容器钉（P0-caps）：三个容器必须在 DOM；
        #       display:none 是合法态（platform_modes 未配置 / 旧进程未回
        #       platform_caps 字段），可见性由数据决定，刻意不钉 ─────────────
        print("== 17. 能力清单/封顶容器钉（#rps-cap-note / #rps-caps-*）==")
        caps_dom = page.evaluate(
            "() => ({note: !!document.getElementById('rps-cap-note'),"
            "        mr: !!document.getElementById('rps-caps-markread'),"
            "        ty: !!document.getElementById('rps-caps-typing')})")
        ck.check("17. 封顶提示 + 已读/打字能力清单容器都在 DOM",
                 caps_dom["note"] and caps_dom["mr"] and caps_dom["ty"],
                 f"note={caps_dom['note']} markread={caps_dom['mr']}"
                 f" typing={caps_dom['ty']}")

        # ── 18. P2 额度组（2026-08-29）：只读 DOM/computed；不点全自动、不点保存
        print("== 18. 额度组 / 发送闸门卡 / 快捷档 / sticky 锚点 ==")
        ck.check("18a #rps-grp-quota 在 DOM",
                 page.evaluate("() => !!document.getElementById('rps-grp-quota')"))
        sg_disp = page.evaluate(
            "() => { const el = document.getElementById('rps-sec-sendgate');"
            " if (!el) return null;"
            " return getComputedStyle(el).display; }")
        ck.check("18b #rps-sec-sendgate 可见（非 display:none）",
                 sg_disp not in (None, "none"), f"display={sg_disp!r}")
        try:
            page.wait_for_function(
                "() => { const b = document.getElementById('rps-sg-body');"
                " return b && getComputedStyle(b).display !== 'none'; }",
                timeout=8000)
            sg_body = True
        except Exception:
            sg_body = False
        ck.check("18c feat 装载后 #rps-sg-body 可见",
                 sg_body,
                 "" if sg_body else "8s 内仍 hidden——白名单未装载或 rpsLoad 失败")
        ck.check("18d 出厂档按钮 rpsGuardQuick(500) 在 DOM",
                 page.evaluate(
                     "() => Array.from(document.querySelectorAll("
                     "'#rps-sec-guard button.rps-guard-quick'))"
                     ".some(b => (b.getAttribute('onclick') || '')"
                     ".indexOf('rpsGuardQuick(500)') >= 0)"))
        sticky = page.evaluate(
            "() => { const n = document.querySelector('.rps-anchors');"
            " return n ? getComputedStyle(n).position : null; }")
        ck.check("18e .rps-anchors position=sticky",
                 sticky == "sticky", f"position={sticky!r}")
        gh3 = page.evaluate(
            "() => { const h = document.querySelector('#rps-sec-guard h3');"
            " return h ? (h.textContent || '') : ''; }")
        ck.check("18f 守卫 h3 含「单会话额度」",
                 "单会话额度" in gh3, f"h3={gh3[:48]!r}")
        sh3 = page.evaluate(
            "() => { const h = document.querySelector('#rps-sec-sendgate h3');"
            " return h ? (h.textContent || '') : ''; }")
        ck.check("18g 发送闸门 h3 含「单账号日发」",
                 "单账号日发" in sh3, f"h3={sh3[:48]!r}")
        ck.check("18h 危险确认框 #rps-confirm 在 DOM",
                 page.evaluate("() => !!document.getElementById('rps-confirm')"))
        ck.check("18i 额度卡带 rps-card-tier-quota",
                 page.evaluate(
                     "() => { const el = document.getElementById('rps-sec-sendgate');"
                     " return !!(el && el.classList.contains('rps-card-tier-quota')); }"))

        # 全页截图（诊断产物，PASS/FAIL 都留档）
        if shots:
            page.screenshot(path=str(shots / "reply_settings_verify.png"),
                            full_page=True)

        # ═══ 英文视图走查（P2）：第二个 page，URL 追加 &lang=en ═══════════════
        # lang 查询参数即可强制英文渲染（admin.py 侧 query 优先于 cookie），
        # 同一浏览器上下文复用登录态与 full 模式，仍是纯只读。
        ck.segment = SEG_EN
        en_path = PAGE_PATH + "&lang=en"
        print(f"\n== EN. 英文视图走查（{en_path}）==")
        page_en = ctx.new_page()
        page_en.goto(base + en_path, wait_until="domcontentloaded")
        try:
            page_en.wait_for_selector(".rps-mode-box", timeout=20000)
        except Exception:
            ck.check("EN-0 页面就绪（.rps-mode-box 出现）", False,
                     "20s 超时——英文渲染路径挂了或实例在重启")
            try:
                if shots:
                    page_en.screenshot(
                        path=str(shots / "reply_settings_verify_en.png"),
                        full_page=True)
            except Exception:
                pass
            browser.close()
            return ck.summary()
        page_en.wait_for_timeout(1500)  # 与中文段同口径：等异步渲染就位

        # ── 12. EN 播放按钮钉：守 EN 词条完整性——按钮文案走 i18n 英文词条，
        #       而非漏翻回落中文兜底 ──────────────────────────────────────────
        print("== 12. EN 播放按钮钉（#rps-pv-play 文案）==")
        pv_txt = page_en.evaluate(
            "() => { const b = document.getElementById('rps-pv-play');"
            " return b ? (b.textContent || '') : null; }")
        ck.check("12. EN #rps-pv-play 文案含 'Play preview'",
                 pv_txt is not None and "Play preview" in pv_txt,
                 f"text={(pv_txt or '')[:48]!r}")

        # ── 13. EN 静态标签零 CJK 钉：守 EN 词条完整性——只扫静态标签元素
        #       （h3/.rps-row-label/.rps-anchors a/summary），不扫整页：实测
        #       数据（人设名等）含中文是合法的数据驱动内容，不能误伤 ─────────
        print("== 13. EN 静态标签零 CJK 钉（.rps-wrap 静态标签）==")
        cjk_hits = page_en.evaluate(
            "() => { const wrap = document.querySelector('.rps-wrap');"
            " if (!wrap) return null;"
            " const els = wrap.querySelectorAll("
            "     'h3, .rps-row-label, .rps-anchors a, summary');"
            " const re = /[\\u4e00-\\u9fff]/; const hits = [];"
            " els.forEach(el => { const t = (el.textContent || '').trim();"
            "     if (re.test(t)) hits.push(t.slice(0, 40)); });"
            " return hits; }")
        if cjk_hits is None:
            ck.check("13. EN 静态标签零 CJK", False, "未找到 .rps-wrap")
        else:
            ck.check("13. EN 静态标签零 CJK（h3/.rps-row-label/.rps-anchors a/summary）",
                     len(cjk_hits) == 0,
                     "" if not cjk_hits
                     else f"命中 {len(cjk_hits)} 处，前 5: " + " | ".join(cjk_hits[:5]))

        # ── 14. EN 布局钉：守 EN 网格不破版——英文长标签在 230px 标签列内正常
        #       换行；clientWidth 超 240 = 标签列被长词撑破网格 ────────────────
        print("== 14. EN 布局钉（.rps-row-label 宽度）==")
        lblw = page_en.evaluate(
            "() => { const el = document.querySelector('.rps-row-label');"
            " return el ? el.clientWidth : null; }")
        ck.check("14. EN 首个 .rps-row-label clientWidth <= 240px",
                 lblw is not None and lblw <= 240, f"clientWidth={lblw}px")

        # ── 15. EN 全页截图：英文视图独立留档（诊断产物，PASS/FAIL 都留）─────
        print("== 15. EN 全页截图 ==")
        if shots:
            fp_en = shots / "reply_settings_verify_en.png"
            try:
                page_en.screenshot(path=str(fp_en), full_page=True)
                ck.check("15. EN 全页截图已存档（reply_settings_verify_en.png）",
                         fp_en.exists(), str(fp_en))
            except Exception as e:  # noqa: BLE001
                ck.check("15. EN 全页截图已存档（reply_settings_verify_en.png）",
                         False, str(e)[:80])
        else:
            ck.skip("15. EN 全页截图", "--shots 留空，未启用截图")

        browser.close()
    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="「自动回复设置」页可读性/布局门禁（只读，无副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"实例地址（默认 {DEFAULT_BASE}）")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--shots", default="out", help="截图输出目录（默认 out/；留空不截图）")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = ap.parse_args(argv)

    # Windows 控制台默认 GBK，页面文案里的 emoji / 特殊符号会 UnicodeEncodeError
    # 崩掉（verify_inbox_density 首跑踩过；同族 .ps1 的「ASCII-only」说的同一个坑）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  老 Python / 被重定向的流
        pass

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[SKIP] 未装 playwright（pip install playwright && playwright install chromium）")
        return 0

    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except urllib.error.HTTPError:
        pass          # 任何 HTTP 响应都代表实例活着
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达（{args.base}）：{str(e)[:80]}")
        return 0

    token = args.token or read_token(args.data_root)
    if not token:
        # 与 verify_inbox_density 的 [ABORT] exit 2 刻意不同：本工具按任务规格
        # 把「读不到 token」也归为环境未备好 → SKIP exit 0，不污染回归信号。
        print(f"[SKIP] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 0
    shots = None
    if args.shots:
        shots = Path(args.shots)
        shots.mkdir(parents=True, exist_ok=True)
    print(f"== 0. 目标 {args.base}{PAGE_PATH}"
          f"（视口 {VIEWPORT['width']}x{VIEWPORT['height']} @2x）==")
    return run(args.base, token, shots=shots, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
